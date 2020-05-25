import os
import json
from datetime import datetime
from statistics import mean
import argparse

import numpy as np
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix, accuracy_score
from sklearn.utils.multiclass import unique_labels

import torch
import torch.optim as optim
from torch.autograd import Variable
import torch.nn.functional as F

from models import SqueezeNet, Generator
from datasets import GazeDataset


parser = argparse.ArgumentParser('Options for finetuning GazeNet models in PyTorch...')
parser.add_argument('--dataset-root-path', type=str, default=None, help='path to dataset')
parser.add_argument('--version', type=str, default=None, help='which version of SqueezeNet to load (1_0/1_1)')
parser.add_argument('--output-dir', type=str, default=None, help='output directory for model and logs')
parser.add_argument('--snapshot-dir', type=str, default=None, help='directory with pre-trained model snapshots')
parser.add_argument('--batch-size', type=int, default=32, metavar='N', help='batch size for training')
parser.add_argument('--epochs', type=int, default=50, metavar='N', help='number of epochs to train for')
parser.add_argument('--learning-rate', type=float, default=0.0001, metavar='LR', help='learning rate')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help='momentum for gradient step')
parser.add_argument('--weight-decay', type=float, default=0.0005, metavar='WD', help='weight decay')
parser.add_argument('--log-schedule', type=int, default=10, metavar='N', help='number of iterations to print/save log after')
parser.add_argument('--seed', type=int, default=1, help='set seed to some constant value to reproduce experiments')
parser.add_argument('--no-cuda', action='store_true', default=False, help='do not use cuda for training')
parser.add_argument('--size', type=int, default=256, help='size of the data crop (squared assumed)')
parser.add_argument('--nc', type=int, default=1, help='number of channels of data')
parser.add_argument('--random-transforms', action='store_true', default=False, help='apply random transforms to input while training')


args = parser.parse_args()
# check args
if args.dataset_root_path is None:
    assert False, 'Path to dataset not provided!'
if all(args.version != x for x in ['1_0', '1_1']):
    assert False, 'Model version not recognized!'

# Output class labels
activity_classes = ['Eyes Closed', 'Forward', 'Shoulder', 'Left Mirror', 'Lap', 'Speedometer', 'Radio', 'Rearview', 'Right Mirror']
merged_activity_classes = ['Eyes Closed/Lap', 'Forward', 'Left Mirror', 'Speedometer', 'Radio', 'Rearview', 'Right Mirror']
args.num_classes = len(activity_classes)

# setup args
args.cuda = not args.no_cuda and torch.cuda.is_available()
if args.output_dir is None:
    args.output_dir = datetime.now().strftime("%Y-%m-%d-%H:%M")
    args.output_dir = os.path.join('.', 'experiments', 'gazenet-ft', args.output_dir)

if not os.path.exists(args.output_dir):
    os.makedirs(args.output_dir)
else:
    assert False, 'Output directory already exists!'

# store config in output directory
with open(os.path.join(args.output_dir, 'config.json'), 'w') as f:
    json.dump(vars(args), f)

torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)


def plot_confusion_matrix(y_true, y_pred, classes, normalize=True, title=None, cmap=plt.cm.Blues):
    """
    This function prints and plots the confusion matrix.
    Normalization can be applied by setting `normalize=True`.
    """
    if not title:
        if normalize:
            title = 'Normalized confusion matrix'
        else:
            title = 'Confusion matrix, without normalization'

    # merge "Eyes Closed" and "Lap" classes
    y_true[y_true == 4] = 0
    y_pred[y_pred == 4] = 0
    # change GT "Shoulder" to "Left Mirror"
    y_true[np.logical_and(y_true == 2, y_pred == 3)] = 3
    # change GT "Shoulder" to "Right Mirror"
    y_true[np.logical_and(y_true == 2, y_pred == 8)] = 8
    # change prediction "Shoulder" to "Left Mirror"
    y_pred[np.logical_and(y_pred == 2, y_true == 3)] = 3
    # change prediction "Shoulder" to "Right Mirror"
    y_pred[np.logical_and(y_pred == 2, y_true == 8)] = 8
    # remove "Shoulder" class
    retain = np.logical_and(y_pred != 2, y_true != 2)
    y_true = y_true[retain]
    y_pred = y_pred[retain]
    
    # Compute confusion matrix
    cm = confusion_matrix(y_true, y_pred)
    if normalize:
        cm = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]

    fig, ax = plt.subplots()
    im = ax.imshow(cm, interpolation='nearest', cmap=cmap)
    ax.figure.colorbar(im, ax=ax)
    # We want to show all ticks...
    ax.set(xticks=np.arange(cm.shape[1]),
           yticks=np.arange(cm.shape[0]),
           # ... and label them with the respective list entries
           xticklabels=classes, yticklabels=classes,
           title=title,
           ylabel='True label',
           xlabel='Predicted label')

    # Rotate the tick labels and set their alignment.
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right",
             rotation_mode="anchor")

    # Loop over data dimensions and create text annotations.
    fmt = '.2f' if normalize else 'd'
    thresh = cm.max() / 2.
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, format(cm[i, j], fmt),
                    ha="center", va="center",
                    color="white" if cm[i, j] > thresh else "black")
    fig.tight_layout()
    fig.savefig(os.path.join(args.output_dir, 'confusion_matrix.jpg'))
    return 100.0*accuracy_score(y_true, y_pred)


kwargs = {'batch_size': args.batch_size, 'shuffle': True, 'num_workers': 6}
train_loader = torch.utils.data.DataLoader(GazeDataset(args.dataset_root_path, 'train', args.random_transforms), **kwargs)
val_loader = torch.utils.data.DataLoader(GazeDataset(args.dataset_root_path, 'val', False), **kwargs)

# global var to store best validation accuracy across all epochs
best_accuracy = 0.0


# training function
def train(netGaze, epoch):
    epoch_loss = list()
    pred_all = np.array([], dtype='int64')
    target_all = np.array([], dtype='int64')
    netGaze.train()
    for b_idx, (data, targets) in enumerate(train_loader):
        if args.cuda:
            data, targets = data.cuda(), targets.cuda()
        # convert the data and targets into Variable and cuda form
        data, targets = Variable(data), Variable(targets)

        # train the network
        optimizer.zero_grad()

        scores, masks = netGaze(data)
        scores = scores.view(-1, args.num_classes)
        loss = F.nll_loss(scores, targets)

        # compute the accuracy
        pred = scores.data.max(1)[1]  # get the index of the max log-probability
        pred_all   = np.append(pred_all, pred.cpu().numpy())
        target_all = np.append(target_all, targets.cpu().numpy())

        epoch_loss.append(loss.item())
        loss.backward()
        optimizer.step()

        if b_idx % args.log_schedule == 0:
            print('Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, (b_idx+1) * len(data), len(train_loader.dataset),
                100. * (b_idx+1)*len(data) / len(train_loader.dataset), loss.item()))
            with open(os.path.join(args.output_dir, "logs.txt"), "a") as f:
                f.write('Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}\n'.format(
                epoch, (b_idx+1) * len(data), len(train_loader.dataset),
                100. * (b_idx+1)*len(data) / len(train_loader.dataset), loss.item()))

    # now that the epoch is completed calculate statistics and store logs
    avg_loss = mean(epoch_loss)
    print("------------------------\nAverage loss for epoch = {:.2f}".format(avg_loss))
    with open(os.path.join(args.output_dir, "logs.txt"), "a") as f:
        f.write("\n------------------------\nAverage loss for epoch = {:.2f}\n".format(avg_loss))

    train_accuracy =  100.0*accuracy_score(target_all, pred_all)
    print("Accuracy for epoch = {:.2f}%\n------------------------".format(train_accuracy))
    with open(os.path.join(args.output_dir, "logs.txt"), "a") as f:
        f.write("Accuracy for epoch = {:.2f}%\n------------------------\n".format(train_accuracy))
    
    return netGaze, avg_loss, train_accuracy


# validation function
def val(netGaze, netG_B2A):
    global best_accuracy
    netGaze.eval()
    pred_all = np.array([], dtype='int64')
    target_all = np.array([], dtype='int64')
    
    for idx, (data, target) in enumerate(val_loader):
        if args.cuda:
            data, target = data.cuda(), target.cuda()
        data, target = Variable(data), Variable(target)

        # do the forward pass
        scores = netGaze(data)[0]
        scores = scores.view(-1, args.num_classes)
        pred = scores.data.max(1)[1]  # got the indices of the maximum, match them
        print('Done with image {} out of {}...'.format(min(args.batch_size*(idx+1), len(val_loader.dataset)), len(val_loader.dataset)))
        pred_all   = np.append(pred_all, pred.cpu().numpy())
        target_all = np.append(target_all, target.cpu().numpy())

    val_accuracy =  plot_confusion_matrix(target_all, pred_all, merged_activity_classes)
    print("\n------------------------")
    print("Validation accuracy = {:.2f}%\n------------------------".format(val_accuracy))
    with open(os.path.join(args.output_dir, "logs.txt"), "a") as f:
        f.write("\n------------------------\n")
        f.write("Validation accuracy = {:.2f}%\n------------------------\n".format(val_accuracy))

    # now save the model if it has better accuracy than the best model seen so forward
    if val_accuracy > best_accuracy:
        # save the model
        torch.save(netGaze.state_dict(), os.path.join(args.output_dir, 'netGaze.pth'))
        torch.save(netG_B2A.state_dict(), os.path.join(args.output_dir, 'netG_B2A.pth'))
        best_accuracy = plot_confusion_matrix(target_all, pred_all, merged_activity_classes)

    return val_accuracy


if __name__ == '__main__':
    # get the model, load pretrained weights, and convert it into cuda for if necessary
    netGaze = SqueezeNet(args.version)
    netG_B2A = Generator(args.nc, args.nc)

    if args.snapshot_dir is not None:
        if os.path.exists(os.path.join(args.snapshot_dir, 'netG_B2A.pth')):
            netG_B2A.load_state_dict(torch.load(os.path.join(args.snapshot_dir, 'netG_B2A.pth')), strict=False)
        if os.path.exists(os.path.join(args.snapshot_dir, 'netGaze.pth')):
            netGaze.load_state_dict(torch.load(os.path.join(args.snapshot_dir, 'netGaze.pth')), strict=False)
        if os.path.exists(os.path.join(args.snapshot_dir, 'netGaze_wo.pth')):
            netGaze.load_state_dict(torch.load(os.path.join(args.snapshot_dir, 'netGaze_wo.pth')), strict=False)

    if args.cuda:
        netG_B2A.cuda()
        netGaze.cuda()

    # create a temporary optimizer
    optimizer = optim.SGD(netGaze.parameters(), lr=args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay)

    fig1, ax1 = plt.subplots()
    plt.grid(True)
    train_loss = list()

    fig2, ax2 = plt.subplots()
    plt.grid(True)
    ax2.plot([], 'g', label='Train accuracy')
    ax2.plot([], 'b', label='Validation accuracy')
    ax2.legend()
    train_acc, val_acc = list(), list()
    for i in range(1, args.epochs+1):
        val_acc.append(val(netGaze, netG_B2A))
        netGaze, avg_loss, acc = train(netGaze, i)
        # plot the loss
        train_loss.append(avg_loss)
        ax1.plot(train_loss, 'k')
        fig1.savefig(os.path.join(args.output_dir, "train_loss.jpg"))

        # plot the train and val accuracies
        train_acc.append(acc)
        ax2.plot(train_acc, 'g', label='Train accuracy')
        ax2.plot(val_acc, 'b', label='Validation accuracy')
        fig2.savefig(os.path.join(args.output_dir, 'trainval_accuracy.jpg'))
    plt.close('all')