"""

Test versions of Colanet to differences in output due to code modifications.

"""

# -- misc --
import sys,tqdm,pytest,math,random
from pathlib import Path

# -- dict data --
import copy
from easydict import EasyDict as edict

# -- vision --
from PIL import Image

# -- testing --
import unittest
import tempfile

# -- linalg --
import torch as th
import numpy as np
from einops import rearrange,repeat

# -- data --
import data_hub

# -- package imports [to test] --
import dnls # supporting
from torchvision.transforms.functional import center_crop

# -- package imports [to test] --
import colanet
import colanet.utils.gpu_mem as gpu_mem
from colanet.utils.gpu_mem import print_gpu_stats,print_peak_gpu_stats
import colanet.utils.metrics as metrics

# -- check if reordered --
from scipy import optimize
MAX_NFRAMES = 85
DATA_DIR = Path("./data/")
SAVE_DIR = Path("./output/tests/test_denose_rgb/")
if not SAVE_DIR.exists():
    SAVE_DIR.mkdir(parents=True)

def set_seed(seed):
    random.seed(seed)
    th.manual_seed(seed)
    np.random.seed(seed)
    # th.use_deterministic_algorithms(True)

def pytest_generate_tests(metafunc):
    seed = 123
    set_seed(seed)
    # test_lists = {"ps":[3],"stride":[2],"dilation":[2],
    #               "top":[3],"btm":[57],"left":[7],"right":[57]}
    test_lists = {"sigma":[50.]}
    for key,val in test_lists.items():
        if key in metafunc.fixturenames:
            metafunc.parametrize(key,val)

# -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
#
# -->  Test original vs refactored code base  <--
#
# -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

# @pytest.mark.skip()
def test_original_refactored(sigma):

    # -- params --
    device = "cuda:0"
    vid_set = "bsd68"
    vid_set = "set8"
    verbose = True
    mtype = "gray"
    ensemble = False
    chop = False

    # -- setup cfg --
    cfg = edict()
    cfg.dname = vid_set
    cfg.vid_name = "motorbike"
    cfg.bw = True
    cfg.sigma = 50.

    # -- search space params --
    ws,wt = 29,0

    # -- adaptation params --
    internal_adapt_nsteps = 500
    internal_adapt_nepochs = 1

    # -- batching params --
    batch_size = -1 # unused

    # -- video --
    data,loaders = data_hub.sets.load(cfg)
    groups = data.te.groups
    if "vid_name" in cfg:
        indices = [i for i,g in enumerate(groups) if cfg.vid_name in g]
        index = indices[0]
    else: index = 0

    # -- create timer --
    timer = colanet.utils.timer.ExpTimer()

    # -- unpack --
    sample = data.te[index]
    noisy,clean = sample['noisy'],sample['clean']
    noisy,clean = noisy.to(device),clean.to(device)
    # t,h,w = 4,128,128
    t,h,w = 4,256,256
    noisy = noisy[:t,:,:h,:w].contiguous()
    clean = clean[:t,:,:h,:w].contiguous()
    # noisy = noisy[:,:,:128,:128].contiguous()
    # clean = clean[:,:,:128,:128].contiguous()
    noisy /= 255.
    clean /= 255.
    noisy = noisy[:,[0]].contiguous()
    clean = clean[:,[0]].contiguous()
    print("noisy.shape: ",noisy.shape)
    print("clean.shape: ",noisy.shape)
    # noisy = th.cat([noisy,noisy])
    # clean = th.cat([clean,clean])
    # noisy = th.cat([noisy,noisy],-1)
    # clean = th.cat([clean,clean],-1)
    # noisy = th.cat([noisy,noisy],-2)
    # clean = th.cat([clean,clean],-2)
    dnls.testing.data.save_burst(noisy,"./output","noisy")
    dnls.testing.data.save_burst(clean,"./output","clean")

    print("noisy.shape: ",noisy.shape)

    # -- compute flow --
    flows = None

    # -- get noisy images --
    noisy_og = noisy.clone()
    noisy_og.requires_grad_(True)
    noisy_ref = noisy.clone()
    noisy_ref.requires_grad_(True)

    # -- original exec --
    og_model = colanet.original.load_model(mtype,sigma).eval()
    og_model.chop = chop
    timer.start("original")
    gpu_mem.reset_peak_gpu_stats()
    with th.no_grad():
        deno_og = og_model(noisy_og,0,ensemble=ensemble).detach()
    gpu_mem.print_peak_gpu_stats(True,"og",reset=True)
    # og_model.train()
    # deno_og = og_model(noisy,0,ensemble=ensemble)
    # loss = th.sum((deno_og - clean)**2).sum()
    # loss.backward()
    timer.stop("original")

    # -- each version --
    t,c,h,w = noisy.shape
    coords=[0,0,h,w]
    for ref_version in ["ref"]: #["original","ref"]:

        # -- load model --
        ref_model = colanet.refactored.load_model(mtype,sigma).eval()
        ref_model.chop = chop

        # -- optional adapt --
        run_adapt = (internal_adapt_nsteps>0) and (internal_adapt_nepochs>0)
        if run_adapt:
            # ref_model.run_internal_adapt(noisy,sigma,flows=flows,
            #                              ws=ws,wt=wt,batch_size=batch_size,
            #                              nsteps=internal_adapt_nsteps,
            #                              nepochs=internal_adapt_nepochs,
            #                              clean_gt=clean,verbose=True)
            ref_model.run_external_adapt(clean,sigma,flows=flows,
                                         ws=ws,wt=wt,batch_size=batch_size,
                                         nsteps=internal_adapt_nsteps,
                                         nepochs=internal_adapt_nepochs,
                                         noisy_gt=noisy,verbose=True)

        # -- refactored exec --
        timer.start("refactored")
        with th.no_grad():
            deno_ref = ref_model(noisy_ref,0,ensemble=ensemble).detach()
            # deno_ref = ref_model.my_fwd(noisy,ensemble=ensemble).detach()
        gpu_mem.print_peak_gpu_stats(True,"og",reset=True)
        timer.stop("refactored")

        # -- viz --
        if verbose:
            print(deno_og.shape,clean.shape)
            print("og: ",metrics.compute_psnrs(deno_og,clean,1.))
            print("ref: ",metrics.compute_psnrs(deno_ref,clean,1.))
            print(timer)

        # -- test --
        error = th.sum((deno_og - deno_ref)**2).item()
        if verbose: print("error: ",error)
        assert error < 1e-15
