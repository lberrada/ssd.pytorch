# from cuda import set_cuda
from nr import NR

from data import *
from utils.augmentations import SSDAugmentation
from layers.modules import MultiBoxLoss
from ssd import build_ssd
import os
import sys
import time
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.utils.data as data
import numpy as np
import argparse
import logger


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Single Shot MultiBox Detector Training With Pytorch')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--dataset', default='VOC', choices=['VOC', 'COCO'],
                    type=str, help='VOC or COCO')
parser.add_argument('--dataset_root', default=VOC_ROOT,
                    help='Dataset root directory path')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
                    help='Pretrained base model')
parser.add_argument('--batch_size', default=32, type=int,
                    help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('--start_iter', default=0, type=int,
                    help='Resume training at this iter')
parser.add_argument('--num_workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, type=str2bool,
                    help='Use CUDA to train model')
parser.add_argument('--opt', '--optimizer', default='sgd', type=str,
                    help='optimization method')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float,
                    help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
                    help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1, type=float,
                    help='Gamma update for SGD')
parser.add_argument('--visdom', default=False, type=str2bool,
                    help='Use visdom for loss visualization')
parser.add_argument('--save_folder', default='weights/',
                    help='Directory for saving checkpoint models')
args = parser.parse_args()


if torch.cuda.is_available():
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not args.cuda:
        print("WARNING: It looks like you have a CUDA device, but aren't " +
              "using CUDA.\nRun with --cuda for optimal training speed.")
        torch.set_default_tensor_type('torch.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)


def train():
    if args.dataset == 'COCO':
        if args.dataset_root == VOC_ROOT:
            if not os.path.exists(COCO_ROOT):
                parser.error('Must specify dataset_root if specifying dataset')
            print("WARNING: Using default COCO dataset_root because " +
                  "--dataset_root was not specified.")
            args.dataset_root = COCO_ROOT
        cfg = coco
        dataset = COCODetection(root=args.dataset_root,
                                transform=SSDAugmentation(cfg['min_dim'],
                                                          MEANS))
    elif args.dataset == 'VOC':
        if args.dataset_root == COCO_ROOT:
            parser.error('Must specify dataset if specifying dataset_root')
        cfg = voc
        dataset = VOCDetection(root=args.dataset_root,
                               transform=SSDAugmentation(cfg['min_dim'],
                                                         MEANS))

    config = logger.Config(**vars(args))
    config.update(**cfg)

    Loc_Loss = logger.metric.Average()
    Conf_Loss = logger.metric.Average()
    Total_Loss = logger.metric.Simple()
    timer = logger.metric.Timer()
    Step_Size = logger.metric.Average()

    xp_name = 'ssd-{}--eta-{}'.format(args.opt, args.lr)
    if args.visdom:
        config.server = 'http://atlas.robots.ox.ac.uk'
        config.port = 9003
        visdom_opts = {'env': xp_name, 'server': config.server, 'port': config.port}
        plotter = logger.VisdomPlotter(visdom_opts, manual_update=True)
        config.plot_on(plotter)
        Loc_Loss.plot_on(plotter, 'Loss')
        Conf_Loss.plot_on(plotter, 'Loss')
        Total_Loss.plot_on(plotter, 'Loss')
        Step_Size.plot_on(plotter, 'Step-Size')

        plotter.set_win_opts("Step-Size", {'ytype': 'log'})

    ssd_net = build_ssd('train', cfg['min_dim'], cfg['num_classes'])
    net = ssd_net

    if args.cuda:
        net = torch.nn.DataParallel(ssd_net)
        cudnn.benchmark = True

    if args.resume:
        print('Resuming training, loading {}...'.format(args.resume))
        ssd_net.load_weights(args.resume)
    else:
        vgg_weights = torch.load(args.save_folder + args.basenet)
        print('Loading base network...')
        ssd_net.vgg.load_state_dict(vgg_weights)

    if args.cuda:
        net = net.cuda()

    if not args.resume:
        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        ssd_net.extras.apply(weights_init)
        ssd_net.loc.apply(weights_init)
        ssd_net.conf.apply(weights_init)

    if args.opt == 'sgd':
        optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum,
                              weight_decay=args.weight_decay)
    elif args.opt == 'cosgd':
        optimizer = NR(net.parameters(), eta=args.lr)
    else:
        raise RuntimeError
    criterion = MultiBoxLoss(cfg['num_classes'], 0.5, True, 0, True, 3, 0.5,
                             False, args.cuda)

    net.train()
    # loss counters
    print('Loading the dataset...')

    epoch_size = len(dataset) // args.batch_size
    print('Training SSD on:', dataset.name)
    print('Using the specified args:')
    print(args)

    step_index = 0

    data_loader = data.DataLoader(dataset, args.batch_size,
                                  num_workers=args.num_workers,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=False)
    # create batch iterator
    batch_iterator = iter(data_loader)
    max_epochs = cfg['max_iter'] // epoch_size
    iteration = 0
    for epoch in range(1, max_epochs + 1):
        Loc_Loss.reset()
        Conf_Loss.reset()
        Step_Size.reset()
        for images, targets in data_loader:
            iteration += 1

            if iteration in cfg['lr_steps']:
                step_index += 1
                adjust_learning_rate(optimizer, args.gamma, step_index)

            if args.cuda:
                images = images.cuda()
                targets = [t.cuda() for t in targets]

            # forward
            t0 = time.time()
            out = net(images)
            # backprop
            optimizer.zero_grad()
            loss_l, loss_c = criterion(out, targets)
            loss = loss_l + loss_c
            loss.backward()
            optimizer.step(lambda: float(loss))
            t1 = time.time()
            Loc_Loss.update(loss_l, weighting=images.size(0))
            Conf_Loss.update(loss_c, weighting=images.size(0))
            if not isinstance(optimizer, NR):
                Step_Size.update(optimizer.param_groups[0]['lr'], weighting=images.size(0))
            else:
                Step_Size.update(optimizer.step_size)

            if iteration % 10 == 0:

                print('timer: %.4f sec.' % (t1 - t0))
                print('iter ' + repr(iteration) + ' || Loss: %.4f ||' % (loss.item()), end=' ')
                Total_Loss.update(Loc_Loss.value + Conf_Loss.value)
                Loc_Loss.record(forced_legend='loc', forced_event_time=iteration)
                Conf_Loss.record(forced_legend='conf', forced_event_time=iteration)
                Total_Loss.record(forced_legend='total', forced_event_time=iteration)
                Step_Size.record(forced_event_time=iteration)

            if iteration % 100 == 0:
                plotter.update_plots()

            if iteration != 0 and iteration % 5000 == 0:
                print('Saving state, iter:', iteration)
                torch.save(ssd_net.state_dict(), 'weights/ssd300_COCO_' +
                           repr(iteration) + '.pth')
    torch.save(ssd_net.state_dict(),
               args.save_folder + '' + args.dataset + '.pth')


def adjust_learning_rate(optimizer, gamma, step):
    """Sets the learning rate to the initial LR decayed by 10 at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = args.lr * (gamma ** (step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def xavier(param):
    nn.init.xavier_uniform_(param)


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        m.bias.data.zero_()


if __name__ == '__main__':
    train()
