import numpy as np
from numpy.random import RandomState
from random import Random
import random

import torch
import torch.utils.data.distributed
import torchvision
from torchvision import datasets, transforms

from models import *

class Partition(object):
    """ Dataset-like object, but only access a subset of it. """

    def __init__(self, data, index):
        self.data = data
        self.index = index

    def __len__(self):
        return len(self.index)

    def __getitem__(self, index):
        data_idx = self.index[index]
        return self.data[data_idx]

class DataPartitioner(object):
    """ Partitions a dataset into different chunks. """
    def __init__(self, data, sizes=[0.7, 0.2, 0.1], rnd=0, seed=1234, isNonIID=False, alpha=0,
                 dataset=None, print_f=50):
        self.data = data
        self.dataset = dataset

        if isNonIID:
            self.partitions, self.ratio, self.dat_stat, self.endat_size = self.__getDirichletData__(data, sizes,
                                                                                                    alpha, rnd, print_f)

        else:
            self.partitions = [] 
            self.ratio = sizes
            rng = Random() 
            rng.seed(seed) # seed is fixed so same random number is generated
            data_len = len(data) 
            indexes = [x for x in range(0, data_len)] 
            rng.shuffle(indexes)    # Same shuffling (with each seed)

            for frac in sizes: 
                part_len = int(frac * data_len)
                self.partitions.append(indexes[0:part_len])
                indexes = indexes[part_len:]

    def use(self, partition):
        return Partition(self.data, self.partitions[partition])

    def __getNonIIDdata__(self, data, sizes, seed, alpha):
        labelList = data.targets
        rng = Random()
        rng.seed(seed)
        a = [(label, idx) for idx, label in enumerate(labelList)]

        # Same Part
        labelIdxDict = dict()
        for label, idx in a:
            labelIdxDict.setdefault(label,[])
            labelIdxDict[label].append(idx)
        labelNum = len(labelIdxDict)
        labelNameList = [key for key in labelIdxDict]
        labelIdxPointer = [0] * labelNum

        # sizes = number of nodes
        partitions = [list() for i in range(len(sizes))]
        eachPartitionLen= int(len(labelList)/len(sizes))

        # majorLabelNumPerPartition = ceil(labelNum/len(partitions))
        majorLabelNumPerPartition = 2
        basicLabelRatio = alpha
        interval = 1
        labelPointer = 0

        #basic part
        for partPointer in range(len(partitions)):
            requiredLabelList = list()
            for _ in range(majorLabelNumPerPartition):
                requiredLabelList.append(labelPointer)
                labelPointer += interval
                if labelPointer > labelNum - 1:
                    labelPointer = interval
                    interval += 1
            for labelIdx in requiredLabelList:
                start = labelIdxPointer[labelIdx]
                idxIncrement = int(basicLabelRatio*len(labelIdxDict[labelNameList[labelIdx]]))
                partitions[partPointer].extend(labelIdxDict[labelNameList[labelIdx]][start:start+ idxIncrement])
                labelIdxPointer[labelIdx] += idxIncrement

        #random part
        remainLabels = list()
        for labelIdx in range(labelNum):
            remainLabels.extend(labelIdxDict[labelNameList[labelIdx]][labelIdxPointer[labelIdx]:])
        rng.shuffle(remainLabels)
        for partPointer in range(len(partitions)):
            idxIncrement = eachPartitionLen - len(partitions[partPointer])
            partitions[partPointer].extend(remainLabels[:idxIncrement])
            rng.shuffle(partitions[partPointer])
            remainLabels = remainLabels[idxIncrement:]

        return partitions

    def __getDirichletData__(self, data, psizes, alpha, rnd, print_f):
        n_nets = len(psizes)
        K = 10
        labelList = np.array(data.targets)
        min_size = 0
        N = len(labelList)
        rann = RandomState(2020)

        net_dataidx_map = {}
        while min_size < K:
            idx_batch = [[] for _ in range(n_nets)]
            # for each class in the dataset
            for k in range(K):
                idx_k = np.where(labelList == k)[0]
                rann.shuffle(idx_k)
                proportions = rann.dirichlet(np.repeat(alpha, n_nets))
                ## Balance
                proportions = np.array([p*(len(idx_j)<N/n_nets) for p,idx_j in zip(proportions,idx_batch)])
                proportions = proportions/proportions.sum()
                proportions = (np.cumsum(proportions)*len(idx_k)).astype(int)[:-1]
                idx_batch = [idx_j + idx.tolist() for idx_j,idx in zip(idx_batch,np.split(idx_k,proportions))]
                min_size = min([len(idx_j) for idx_j in idx_batch])

        for j in range(n_nets):
            rann.shuffle(idx_batch[j])
            net_dataidx_map[j] = idx_batch[j]
            
        net_cls_counts = {}

        for net_i, dataidx in net_dataidx_map.items():
            unq, unq_cnt = np.unique(labelList[dataidx], return_counts=True)
            tmp = {unq[i]: unq_cnt[i] for i in range(len(unq))}
            net_cls_counts[net_i] = tmp

        local_sizes = []
        for i in range(n_nets):
            local_sizes.append(len(net_dataidx_map[i]))
        local_sizes = np.array(local_sizes)
        weights = local_sizes/np.sum(local_sizes)

        if rnd % print_f == 0:
            print('Data statistics: %s' % str(net_cls_counts))
            print('Data ratio: %s' % str(weights))

        return idx_batch, weights, net_cls_counts, np.sum(local_sizes)

def partition_dataset(size, args, rnd):

    if args.dataset == 'cifar':
        transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))])

        trainset = torchvision.datasets.CIFAR10(root='./data',
                                            train=True, 
                                            download=True, 
                                            transform=transform_train)

        train_loader = torch.utils.data.DataLoader(trainset,
                                               batch_size=64,
                                               shuffle=False,
                                               num_workers=size)
    
        partition_sizes = [1.0 / args.ensize for _ in range(args.ensize)]
        partition = DataPartitioner(trainset, partition_sizes, rnd, isNonIID=args.NIID, alpha=args.alpha,
                                    dataset=args.dataset, print_f=args.print_freq)
        ratio = partition.ratio

        transform_test = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))])

        testset = torchvision.datasets.CIFAR10(root='./data',
                                        train=False, 
                                        download=True, 
                                        transform=transform_test)

        test_loader = torch.utils.data.DataLoader(testset,
                                            batch_size=64, 
                                            shuffle=False, 
                                            num_workers=size)

    elif args.dataset == 'fmnist':
        apply_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.1307,), (0.3081,))])

        trainset = torchvision.datasets.FashionMNIST(root='./data',
                                                train=True,
                                                download=True,
                                                transform=apply_transform)
        train_loader = torch.utils.data.DataLoader(trainset,
                                                   batch_size=64,
                                                   shuffle=False,
                                                   num_workers=size)

        partition_sizes = [1.0 / args.ensize for _ in range(args.ensize)]
        partition = DataPartitioner(trainset, partition_sizes, rnd, isNonIID=args.NIID, alpha=args.alpha,
                                    dataset=args.dataset, print_f=args.print_freq)
        ratio = partition.ratio  # Ratio of data sizes

        testset = torchvision.datasets.FashionMNIST(root='./data',
                                               train=False,
                                               download=True,
                                               transform=apply_transform)
        test_loader = torch.utils.data.DataLoader(testset,
                                                  batch_size=64,
                                                  shuffle=False,
                                                  num_workers=size)

    elif args.dataset == 'emnist':
        apply_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.1307,), (0.3081,))])

        trainset = torchvision.datasets.EMNIST(root='./data',
                                                split = 'digits',
                                                train=True,
                                                download=True,
                                                transform=apply_transform)

        train_loader = torch.utils.data.DataLoader(trainset,
                                                   batch_size=64,
                                                   shuffle=False,
                                                   num_workers=size)

        partition_sizes = [1.0 / args.ensize for _ in range(args.ensize)]
        partition = DataPartitioner(trainset, partition_sizes, rnd, isNonIID=args.NIID, alpha=args.alpha,
                                    dataset=args.dataset, print_f=args.print_freq)
        ratio = partition.ratio  # Ratio of data sizes

        testset = torchvision.datasets.EMNIST(root='./data',
                                                    split= 'digits',
                                                    train=False,
                                                    download=True,
                                                    transform=apply_transform)
        test_loader = torch.utils.data.DataLoader(testset,
                                                  batch_size=64,
                                                  shuffle=False,
                                                  num_workers=size)

    # add more datasets here

    args.img_size = trainset[0][0].shape

    return partition, train_loader, test_loader, ratio, partition.dat_stat, partition.endat_size

def partitiondata_loader(partition, rank, batch_size):
    '''
    single mini-batch loader
    '''
    partition = partition.use(rank)

    data_idx = random.sample(range(len(partition)), k=int(min(batch_size,len(partition))))
    partitioned = torch.utils.data.Subset(partition, indices=data_idx)
    trainbatch_loader = torch.utils.data.DataLoader(partitioned,
                                               batch_size=batch_size,
                                               shuffle=True,
                                               pin_memory=True)
    return trainbatch_loader


def sel_client(DataRatios, cli_loss, cli_val, args, rnd):
    '''
    Client selection part returning the indices the set $\mathcal{S}$ and $\mathcal{A}$
    :param DataRatios: $p_k$
    :param cli_loss: actual local loss F_k(w)
    :param cli_val: proxy of the local loss
    :param args: variable arguments
    :param rnd: communication round index
    :return: idxs_users (indices of $\mathcal{S}$), rnd_idx (indices of $\mathcal{A}$)
    '''
    # If reproducibility is needed
    #rng1 = Random()
    #rng1.seed(seed)

    rnd_idx = []
    if args.seltype == 'rand':
        # random selection in proportion to $p_k$ with replacement
        idxs_users = np.random.choice(args.ensize, p=DataRatios, size=args.size, replace=True)

    elif args.seltype == 'randint':
        # 'rand' for intermittent client availability
        delete = 0.2
        if (rnd % 2) == 0:
            del_idx = np.random.choice(int(args.ensize / 2), size=int(delete * args.ensize / 2), replace=False)
            search_idx = np.delete(np.arange(0, args.ensize / 2), del_idx)
        else:
            del_idx = np.random.choice(np.arange(args.ensize / 2, args.ensize), size=int(delete * args.ensize / 2),
                                       replace=False)
            search_idx = np.delete(np.arange(args.ensize / 2, args.ensize), del_idx)

        idxs_users = np.random.choice(search_idx, p=[DataRatios[int(i)] for i in search_idx]/sum([DataRatios[int(i)]
                                                                for i in search_idx]), size=args.size, replace=True)

    elif args.seltype == 'pow-d':
        # standard power-of-choice strategy
        rnd_idx = np.random.choice(args.ensize, p=DataRatios, size=args.powd, replace=False)
        repval = list(zip([cli_loss[i] for i in rnd_idx], rnd_idx))
        repval.sort(key=lambda x: x[0], reverse=True)
        rep = list(zip(*repval))
        idxs_users = rep[1][:int(args.size)]

    elif args.seltype == 'rpow-d':
        # computation/communication efficient variant of 'pow-d'
        rnd_idx1 = np.random.choice(args.ensize, p=DataRatios, size=args.powd, replace=False)
        repval = list(zip([cli_val[i] for i in rnd_idx1], rnd_idx1))
        repval.sort(key=lambda x: x[0], reverse=True)
        rep = list(zip(*repval))
        idxs_users = rep[1][:int(args.size)]

    elif args.seltype == 'pow-dint':
        # 'pow-d' for intermittent client availability
        delete = 0.2
        if (rnd % 2) == 0:
            del_idx = np.random.choice(int(args.ensize/2), size=int(delete*args.ensize/2),replace=False)
            search_idx = list(np.delete(np.arange(0,args.ensize/2), del_idx))
        else:
            del_idx = np.random.choice(np.arange(args.ensize/2, args.ensize), size=int(delete*args.ensize/2),
                                       replace=False)
            search_idx = list(np.delete(np.arange(args.ensize/2, args.ensize), del_idx))

        rnd_idx = np.random.choice(search_idx, p=[DataRatios[int(i)] for i in search_idx]/sum([DataRatios[int(i)]
                                                            for i in search_idx]), size=args.powd, replace=False)

        repval = list(zip([cli_loss[int(i)] for i in rnd_idx], rnd_idx))
        repval.sort(key=lambda x: x[0], reverse=True)
        rep = list(zip(*repval))
        idxs_users = rep[1][:int(args.size)]

    elif args.seltype == 'rpow-dint':
        # 'rpow-d' for intermittent client availability
        delete = 0.2
        if (rnd % 2) == 0:
            del_idx = np.random.choice(int(args.ensize/2), size=int(delete*args.ensize/2),replace=False)
            search_idx = list(np.delete(np.arange(0,args.ensize/2), del_idx))
        else:
            del_idx = np.random.choice(np.arange(args.ensize/2, args.ensize), size=int(delete*args.ensize/2),
                                       replace=False)
            search_idx = list(np.delete(np.arange(args.ensize/2, args.ensize), del_idx))

        rnd_idx = np.random.choice(search_idx, p=[DataRatios[int(i)] for i in search_idx]/sum([DataRatios[int(i)]
                                                            for i in search_idx]), size=args.powd, replace=False)

        repval = list(zip([cli_val[int(i)] for i in rnd_idx], rnd_idx))
        repval.sort(key=lambda x: x[0], reverse=True)
        rep = list(zip(*repval))
        idxs_users = rep[1][:int(args.size)]

    elif args.seltype == 'afl':
        # benchmark strategy
        soft_temp = 0.01
        sorted_loss_idx = np.argsort(cli_val)

        for j in sorted_loss_idx[:int(args.delete_ratio*args.ensize)]:
            cli_val[j]=-np.inf

        loss_prob = np.exp(soft_temp*cli_val)/sum(np.exp(soft_temp*cli_val))
        idx1 = np.random.choice(int(args.ensize), p=loss_prob, size = int(np.floor((1-args.rnd_ratio)*args.size)),
                                replace=False)

        new_idx = np.delete(np.arange(0,args.ensize),idx1)
        idx2 = np.random.choice(new_idx, size = int(args.size-np.floor((1-args.rnd_ratio)*args.size)), replace=False)

        idxs_users = list(idx1)+list(idx2)


    return idxs_users, rnd_idx

def choices(population, weights=None, cum_weights=None, k=1):
    """Return a k sized list of population elements chosen with replacement.
    If the relative weights or cumulative weights are not specified,
    the selections are made with equal probability.
    """

    if cum_weights is None:
        if weights is None:
            total = len(population)
            result = []
            for i in range(k):
                random.seed(i)
                result.extend(population[int(random.random() * total)])
            return result
        cum_weights = []
        c = 0
        for x in weights:
            c += x
            cum_weights.append(c)
    elif weights is not None:
        raise TypeError('Cannot specify both weights and cumulative weights')
    if len(cum_weights) != len(population):
        raise ValueError('The number of weights does not match the population')
    total = cum_weights[-1]
    hi = len(cum_weights) - 1
    from bisect import bisect
    result = []
    for i in range(k):
        random.seed(i)
        result.extend(population[bisect(cum_weights, random.random() * total, 0, hi)])
    return result

class Meter(object):
    """ Computes and stores the average, variance, and current value """

    def __init__(self, init_dict=None, ptag='Time', stateful=False,
                 csv_format=True):
        """
        :param init_dict: Dictionary to initialize meter values
        :param ptag: Print tag used in __str__() to identify meter
        :param stateful: Whether to store value history and compute MAD
        """
        self.reset()
        self.ptag = ptag
        self.value_history = None
        self.stateful = stateful
        if self.stateful:
            self.value_history = []
        self.csv_format = csv_format
        if init_dict is not None:
            for key in init_dict:
                try:
                    # TODO: add type checking to init_dict values
                    self.__dict__[key] = init_dict[key]
                except Exception:
                    print('(Warning) Invalid key {} in init_dict'.format(key))

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
        self.std = 0
        self.sqsum = 0
        self.mad = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count
        self.sqsum += (val ** 2) * n
        if self.count > 1:
            self.std = ((self.sqsum - (self.sum ** 2) / self.count)
                        / (self.count - 1)
                        ) ** 0.5
        if self.stateful:
            self.value_history.append(val)
            mad = 0
            for v in self.value_history:
                mad += abs(v - self.avg)
            self.mad = mad / len(self.value_history)

    def __str__(self):
        if self.csv_format:
            if self.stateful:
                return str('{dm.val:.3f},{dm.avg:.3f},{dm.mad:.3f}'
                           .format(dm=self))
            else:
                return str('{dm.val:.3f},{dm.avg:.3f},{dm.std:.3f}'
                           .format(dm=self))
        else:
            if self.stateful:
                return str(self.ptag) + \
                       str(': {dm.val:.3f} ({dm.avg:.3f} +- {dm.mad:.3f})'
                           .format(dm=self))
            else:
                return str(self.ptag) + \
                       str(': {dm.val:.3f} ({dm.avg:.3f} +- {dm.std:.3f})'
                           .format(dm=self))
