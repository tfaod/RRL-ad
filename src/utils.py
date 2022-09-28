# https://github.com/facebookresearch/swav/blob/main/src/utils.py
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import argparse
from logging import getLogger
import pickle
import os

import numpy as np
import torch

from .logger import create_logger, PD_Stats

import torch.distributed as dist

FALSY_STRINGS = {"off", "false", "0"}
TRUTHY_STRINGS = {"on", "true", "1"}


logger = getLogger()


def bool_flag(s):
    """
    Parse boolean arguments from the command line.
    """
    if s.lower() in FALSY_STRINGS:
        return False
    elif s.lower() in TRUTHY_STRINGS:
        return True
    else:
        raise argparse.ArgumentTypeError("invalid value for a boolean flag")

def add_weight_decay(model, weight_decay=1e-5, bnname='bn', skip_list=()):
    decay = []
    no_decay = []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        if bnname in name.lower() or name in skip_list:
            print(name)
            no_decay.append(param)
        else:
            decay.append(param)
    return [
        {'params': no_decay, 'weight_decay': 0.},
        {'params': decay, 'weight_decay': weight_decay}]

 
def init_distributed_mode(args):
    """
    Initialize the following variables:
        - world_size
        - rank
    """
    if args.debug:
        #print(torch.cuda.device_count())
        #if torch.cuda.device_count() > 1: 
        #    os.environ['CUDA_VISIBLE_DEVICES']='1'
        #else:
        #    os.environ['CUDA_VISIBLE_DEVICES']='0'
        #print(torch.cuda.device_count())
        dist.init_process_group(backend="nccl",
        init_method='tcp://127.0.0.1:%d' % (2000 + np.random.randint(20000)),
        world_size=1,
        rank=0,
        )
        if args.gpu is not None:
            args.gpu_to_work_on = args.gpu
        else:
            args.gpu_to_work_on = 0
        torch.cuda.set_device(args.gpu_to_work_on)

    else:
        args.is_slurm_job = "SLURM_JOB_ID" in os.environ

        if args.is_slurm_job:
            args.rank = int(os.environ["SLURM_PROCID"])
            args.world_size = int(os.environ["SLURM_NNODES"]) * int(
                os.environ["SLURM_TASKS_PER_NODE"][0]
            )
        else:
            # multi-GPU job (local or multi-node) - jobs started with torch.distributed.launch
            # read environment variables
            args.rank = int(os.environ["RANK"])
            args.world_size = int(os.environ["WORLD_SIZE"])

        # prepare distributed
        # print(args.dist_url)
        #gpu_ids = os.environ['SLURM_STEP_GPUS'].split(",")
        nodelist = os.environ['SLURM_JOB_NODELIST']
        if '[' in nodelist:
            tmp = nodelist.split('[')
            assert len(tmp) == 2 

            master_addr = tmp[0] + tmp[1][:-1].replace('-',',').split(',')[0]
        else:
            master_addr = nodelist

        os.environ['MASTER_ADDR'] = master_addr
        os.environ['MASTER_PORT'] = str(22451) # to avoid port conflict on the same node

        dist.init_process_group(
            backend="nccl",
            init_method=args.dist_url,
            world_size=args.world_size,
            rank=args.rank,
        )
        # set cuda device
        args.gpu_to_work_on = args.rank % torch.cuda.device_count()
        torch.cuda.set_device(args.gpu_to_work_on)

        # amd GPU only 
        os.environ['MIOPEN_USER_DB_PATH']=os.path.join(args.dump_path, 'rank_%d' % args.rank)

    return


def initialize_exp(params, *args, dump_params=True):
    """
    Initialize the experience:
    - dump parameters
    - create checkpoint repo
    - create a logger
    - create a panda object to keep track of the training statistics
    """

    if not params.rank and not os.path.isdir(params.dump_path):
        os.makedirs(params.dump_path)

    # dump parameters
    if dump_params:
        pickle.dump(params, open(os.path.join(params.dump_path, "params.pkl"), "wb"))

    # create repo to store checkpoints
    params.dump_checkpoints = os.path.join(params.dump_path, "checkpoints")
    if not params.rank and not os.path.isdir(params.dump_checkpoints):
        os.mkdir(params.dump_checkpoints)

    # create a panda object to log loss and acc
    training_stats = PD_Stats(
        os.path.join(params.dump_path, "stats" + str(params.rank) + ".pkl"), args
    )

    # create a logger
    logger = create_logger(
        os.path.join(params.dump_path, "train.log"), rank=params.rank
    )
    logger.info("============ Initialized logger ============")
    logger.info(
        "\n".join("%s: %s" % (k, str(v)) for k, v in sorted(dict(vars(params)).items()))
    )
    logger.info("The experiment will be stored in %s\n" % params.dump_path)
    logger.info("")
    return logger, training_stats


def restart_from_checkpoint(ckp_paths, run_variables=None, **kwargs):
    """
    Re-start from checkpoint
    """
    # look for a checkpoint in exp repository
    if isinstance(ckp_paths, list):
        for ckp_path in ckp_paths:
            if os.path.isfile(ckp_path):
                break
    else:
        ckp_path = ckp_paths

    if not os.path.isfile(ckp_path):
        return

    logger.info("Found checkpoint at {}".format(ckp_path))

    # open checkpoint file
    checkpoint = torch.load(
        ckp_path, map_location="cuda:" + str(torch.distributed.get_rank() % torch.cuda.device_count())
    )

    # key is what to look for in the checkpoint file
    # value is the object to load
    # example: {'state_dict': model}
    for key, value in kwargs.items():
        if key in checkpoint and value is not None:
            try:
                #print('------')
                #print(value)
                #print(checkpoint[key])
                msg = value.load_state_dict(checkpoint[key], strict=False)
                print(msg)
            except TypeError:
                msg = value.load_state_dict(checkpoint[key])
            #print(checkpoint[key])
            print(value)
            logger.info("=> loaded {} from checkpoint '{}'".format(key, ckp_path))
        else:
            logger.warning(
                "=> failed to load {} from checkpoint '{}'".format(key, ckp_path)
            )

    # re load variable important for the run
    if run_variables is not None:
        for var_name in run_variables:
            if var_name in checkpoint:
                run_variables[var_name] = checkpoint[var_name]


def fix_random_seeds(seed=31):
    """
    Fix random seeds.
    """
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)


class AverageMeter(object):
    """computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = min(max(topk), output.shape[1])
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            if len(correct) >= k:
                correct_k = correct[:k].flatten().float().sum(0, keepdim=True)

                res.append(correct_k.mul_(100.0 / batch_size))
            else:
                res.append(torch.Tensor([0]).float().to(correct.get_device()))
        return res
def add_slurm_params(parser):
    #########################
    #### slurm parameters ###
    #########################
    parser.add_argument("--hours", default=12, type=float,
                        help="how many hours to run")
    parser.add_argument("--tasks_per_node", default=8, type=int, help='task per node')
    parser.add_argument("--cpus_per_task", default=8, type=int, help='cpus per task')
    parser.add_argument("--gpus_per_node", default=1, type=int, help='gpus per node')
    parser.add_argument("--nodes", default=1, type=int, help='number of nodes')
    parser.add_argument("--mem", default='64G', type=str, help='memory')
    parser.add_argument('--constraint',default=None,type=str,help='constraint')
    parser.add_argument('--gres',default=None,type=str,help='gres')
    parser.add_argument('--exclude',default=None,type=str,help='exclude')
    parser.add_argument('--job_name',default='submitit',type=str,help='jobname')
    
    return parser


from torch.utils.data.distributed import DistributedSampler
import numpy as np 

class DistributedWeightedSampler(DistributedSampler):
    def __init__(self,*args, **kwargs):
        super().__init__(*args,**kwargs)
        self.weights = None 
        assert self.shuffle == True, "weightedsampler has to suffle the dataset"
    
    def set_weights(self,weights):
        self.weights = weights

    def __iter__(self):
        
        g = np.random.default_rng(self.seed + self.epoch)
        indices = g.choice(len(self.dataset), size=len(self.dataset), replace=True, p=self.weights).tolist()

        if not self.drop_last:
            # add extra samples to make it evenly divisible
            padding_size = self.total_size - len(indices)
            if padding_size <= len(indices):
                indices += indices[:padding_size]
            else:
                indices += (indices * math.ceil(padding_size / len(indices)))[:padding_size]
        else:
            # remove tail of data to make it evenly divisible.
            indices = indices[:self.total_size]
        assert len(indices) == self.total_size

        # subsample
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert len(indices) == self.num_samples

        return iter(indices)





class DistributedSequenceSampler(DistributedSampler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args,**kwargs)
        assert self.shuffle == False, "weightedsampler has to suffle the dataset"
    
    def __iter__(self):
        
        idx = np.arange(len(self.dataset))
        per_size = (len(self.dataset) // self.num_replicas) + 1 

        indices = idx[self.rank * per_size: (self.rank+1) * per_size ]
        
        return iter(indices)



class DistributedGroupSampler(DistributedSampler):
    def __init__(self,groups, n_groups_per_batch=2, batch_size=32, *args, **kwargs):
        super().__init__(*args,**kwargs)
        from wilds.common.utils import split_into_groups

        #self.uniform_over_groups = uniform_over_groups 
        self.n_groups_per_batch = n_groups_per_batch 
        self.groups = groups 
        _, self.group_indices, _ = split_into_groups(groups)
        self.groupsize =[len(var) for var in self.group_indices]
        self.batch_size = batch_size
        assert self.batch_size % self.n_groups_per_batch == 0
    def __iter__(self):
        per_group_size = len(self.groups) // len(self.group_indices)
        #n_add_examples = len(self.groups) // len(self.group_indices) - self.groupsize
        g = np.random.default_rng(self.seed + self.epoch)

        indices = []
        print('group size', self.groupsize)
        for i in range(np.array(self.groupsize).sum() // self.batch_size):
            gids = g.choice(np.arange(len(self.group_indices)),self.n_groups_per_batch,replace=False)
            #print(gids)
            for gid in gids:
                index = g.choice(self.group_indices[gid], self.batch_size // self.n_groups_per_batch, replace=False)
                indices.extend(index.tolist())


        if not self.drop_last:
            # add extra samples to make it evenly divisible
            padding_size = self.total_size - len(indices)
            if padding_size <= len(indices):
                indices += indices[:padding_size]
            else:
                indices += (indices * math.ceil(padding_size / len(indices)))[:padding_size]
        else:
            # remove tail of data to make it evenly divisible.
            indices = indices[:self.total_size]
        assert len(indices) == self.total_size

        # subsample
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert len(indices) == self.num_samples

        #@indices = indices[:int(len(indices)//10)]
        return iter(indices)











