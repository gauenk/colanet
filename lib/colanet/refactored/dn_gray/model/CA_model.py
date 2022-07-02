import dnls
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import gradcheck
from einops import rearrange,repeat

"""
fundamental functions
"""
def same_padding(images, ksizes, strides, rates):
    assert len(images.size()) == 4
    batch_size, channel, rows, cols = images.size()
    out_rows = (rows + strides[0] - 1) // strides[0]
    out_cols = (cols + strides[1] - 1) // strides[1]
    effective_k_row = (ksizes[0] - 1) * rates[0] + 1
    effective_k_col = (ksizes[1] - 1) * rates[1] + 1
    padding_rows = max(0, (out_rows - 1) * strides[0] + effective_k_row - rows)
    padding_cols = max(0, (out_cols - 1) * strides[1] + effective_k_col - cols)
    # Pad the input
    padding_top = int(padding_rows / 2.)
    padding_left = int(padding_cols / 2.)
    padding_bottom = padding_rows - padding_top
    padding_right = padding_cols - padding_left
    paddings = (padding_left, padding_right, padding_top, padding_bottom)
    images = torch.nn.ZeroPad2d(paddings)(images)
    return images, paddings


def extract_image_patches(images, ksizes, strides, rates, padding='same', coords=None):
    """
    Extract patches from images and put them in the C output dimension.
    :param padding:
    :param images: [batch, channels, in_rows, in_cols]. A 4-D Tensor with shape
    :param ksizes: [ksize_rows, ksize_cols]. The size of the sliding window for
     each dimension of images
    :param strides: [stride_rows, stride_cols]
    :param rates: [dilation_rows, dilation_cols]
    :return: A Tensor
    """
    assert len(images.size()) == 4
    assert padding in ['same', 'valid']
    paddings = (0, 0, 0, 0)

    if padding == 'same':
        images, paddings = same_padding(images, ksizes, strides, rates)
    elif padding == 'valid':
        pass
    else:
        raise NotImplementedError('Unsupported padding type: {}.\
                Only "same" or "valid" are supported.'.format(padding))
    # print("images.shape: ",images.shape,ksizes,strides)

    t = images.shape[0]
    ksize = ksizes[0]
    stride = strides[0]
    print("ksize,stride: ",ksize,stride)
    unfold = dnls.iunfold.iUnfold(ksize,coords,stride=stride,dilation=1,adj=True)
    patches = unfold(images)
    patches = rearrange(patches,'(t n) 1 1 c h w -> t (c h w) n',t=t)
    print("[1] patches.shape: ",patches.shape)

    # folder = dnls.ifold.iFold((T,C,H,W),coords,stride=stride,dilation=1,adj=True)

    unfold = torch.nn.Unfold(kernel_size=ksizes,padding=0,stride=strides)
    patches = unfold(images)
    print("[2] patches.shape: ",patches.shape)

    return patches, paddings

"""
CA network
"""
class ContextualAttention_Enhance(nn.Module):
    def __init__(self, ksize=7, stride_1=4, stride_2=1, softmax_scale=10,shape=64 ,p_len=64,in_channels=64
                 , inter_channels=16,use_multiple_size=False,use_topk=False,add_SE=False):
        super(ContextualAttention_Enhance, self).__init__()
        self.ksize = ksize
        self.shape=shape
        self.p_len=p_len
        self.stride_1 = stride_1
        self.stride_2 = stride_2
        self.softmax_scale = softmax_scale
        self.inter_channels = inter_channels
        self.in_channels = in_channels
        self.use_multiple_size=use_multiple_size
        self.use_topk=use_topk
        self.add_SE=add_SE
        # self.SE=SE_net(in_channels=in_channels)
        self.conv33=nn.Conv2d(in_channels=2*in_channels,out_channels=in_channels,kernel_size=1,stride=1,padding=0)
        self.g = nn.Conv2d(in_channels=self.in_channels, out_channels=self.inter_channels, kernel_size=1, stride=1,
                           padding=0)
        self.W = nn.Conv2d(in_channels=self.inter_channels, out_channels=self.in_channels, kernel_size=1, stride=1,
                           padding=0)
        self.theta = nn.Conv2d(in_channels=self.in_channels, out_channels=self.inter_channels, kernel_size=1, stride=1,
                               padding=0)
        self.phi = nn.Conv2d(in_channels=self.in_channels, out_channels=self.inter_channels, kernel_size=1, stride=1,
                             padding=0)

    def dnls_forward(self, b, coords=None):

        # -- get images --
        b1 = self.g(b)
        b2 = self.theta(b)
        b3 = self.phi(b)

        # -- unpack parameters --
        t,c,h,w = b1.shape
        kernel = self.ksize
        vshape = b1.shape
        ps,stride = self.ksize,self.stride_1
        chnls = b2.shape[1]
        dil,adj = 1,True
        pt,ws,wt = 1,-1,-1
        coords = [0,0,h,w] if coords is None else coords
        device = b.device

        # -- get search size --
        cr_h = coords[2] - coords[0]
        cr_w = coords[3] - coords[1]

        # -- batching params --
        nh = (cr_h-1)//stride+1
        nw = (cr_w-1)//stride+1
        ntotal = t * nh * nw
        nbatch = 512
        nbatches = (ntotal-1) // nbatch + 1

        # -- offsets --
        oh0,ow0,oh1,ow1 = 3,3,1,1

        # -- define functions --
        ifold = dnls.ifold.iFold(vshape,coords,stride=stride,dilation=dil,adj=adj)
        wfold = dnls.ifold.iFold(vshape,coords,stride=stride,dilation=dil,adj=adj)
        iunfold = dnls.iunfold.iUnfold(ps,coords,stride=stride,dilation=dil,adj=adj)
        xsearch = dnls.xsearch.CrossSearchNl(None, None, -1, ps, pt, ws, wt,
                                             oh0, ow0, oh1, ow1,
                                             chnls=chnls,dilation=dil, stride=stride)

        # -- batch across queries --
        for index in range(nbatches):

            # -- batch info --
            qindex = min(nbatch * index,ntotal)
            nbatch_i =  min(nbatch, ntotal - qindex)

            # -- get patches --
            iqueries = dnls.utils.inds.get_iquery_batch(qindex,nbatch_i,stride,
                                                        coords,t,device)

            # -- search --
            nlDists_cu,nlInds_cu = xsearch(b1,b2,iqueries)

            # -- attn mask --
            yi = F.softmax(nlDists_cu*self.softmax_scale,1)

            # -- scatter new vid type --
            patches = iunfold(b3,0,nbatch)
            zi = yi @ patches
            ones = th.ones_like(zi)

            # -- fold into videos --
            ifold(zi,qindex)
            wfold(ones,qindex)

        # -- get post-attn vid --
        y = ifold.vid
        Z = wfold.vid
        y = y / Z

        # -- final transform --
        y = self.W(y)
        y = b + y

        if self.add_SE:
            y_SE=self.SE(y)
            y=self.conv33(torch.cat((y_SE*y,y),dim=1))

        return y

        # # -- compute cross-scale search inplace --
        # # fold,wfold = dnls.ifold.iFold(),dnls.ifold.iFold()
        # fold,unfold = th.nn.functional.fold,th.nn.functional.unfold
        # # unfold = dnls.iunfold.iUnfold(ksize,coords,stride=stride,dilation=1,adj=True)
        # scatter = dnls.scatter_nl(scale=1)
        # dnls_search = dnls.xsearch.CrossScaleSearch(flows.fflow, flows.bflow, k, ps, pt,
        #                                             ws, wt, chnls=chnls,dilation=1, stride=1)

        # yi = F.softmax(nlDists_cu*self.softmax_scale,1)
        # patches = scatter_nl(x,queryInds)
        # zi = yi @ patches
        # ones = th.ones_like(zi)
        # zi = fold(zi)
        # ones = wfold(ones)
        # zi = zi / ones
        # y.append(zi)


    def forward(self, b, coords=None):

        kernel = self.ksize
        coords = None

        b1 = self.g(b)
        b2 = self.theta(b)
        b3 = self.phi(b)

        raw_int_bs = list(b1.size())  # b*c*h*w
        coords = coords

        patch_28, paddings_28 = extract_image_patches(b1, ksizes=[self.ksize, self.ksize],
                                                      strides=[self.stride_1, self.stride_1],
                                                      rates=[1, 1],
                                                      padding='same',coords=coords)
        print("patch_28.shape: ",patch_28.shape)
        patch_28 = patch_28.view(raw_int_bs[0], raw_int_bs[1], kernel, kernel, -1)
        patch_28 = patch_28.permute(0, 4, 1, 2, 3)
        patch_28_group = torch.split(patch_28, 1, dim=0)

        patch_112, paddings_112 = extract_image_patches(b2, ksizes=[self.ksize, self.ksize],
                                                        strides=[self.stride_2, self.stride_2],
                                                        rates=[1, 1],
                                                        padding='same',coords=coords)
        print("patch_112.shape: ",patch_112.shape)


        patch_112 = patch_112.view(raw_int_bs[0], raw_int_bs[1], kernel, kernel, -1)
        patch_112 = patch_112.permute(0, 4, 1, 2, 3)
        patch_112_group = torch.split(patch_112, 1, dim=0)

        patch_112_2, paddings_112_2 = extract_image_patches(b3, ksizes=[self.ksize, self.ksize],
                                                        strides=[self.stride_2, self.stride_2],
                                                        rates=[1, 1],
                                                        padding='same',coords=None)


        patch_112_2 = patch_112_2.view(raw_int_bs[0], raw_int_bs[1], kernel, kernel, -1)
        patch_112_2 = patch_112_2.permute(0, 4, 1, 2, 3)
        patch_112_group_2 = torch.split(patch_112_2, 1, dim=0)
        f_groups = torch.split(b3, 1, dim=0)
        print("f_groups.shape: ",[f.shape for f in f_groups])
        plist = [patch_112_group_2, patch_28_group, patch_112_group]
        for p in plist:
            print("shape: ",[gr.shape for gr in p])

        y = []
        # -- process each batch separately --
        for xii,xi, wi,pi in zip(f_groups,patch_112_group_2, patch_28_group, patch_112_group):
            # print("xii,xi,wi,pi: ",xii.shape,xi.shape,wi.shape,pi.shape)
            w,h = xii.shape[2], xii.shape[3]
            _, paddings = same_padding(xii, [self.ksize, self.ksize], [1, 1], [1, 1])
            # wi = wi[0]  # [L, C, k, k]
            c_s = pi.shape[2]
            k_s = wi[0].shape[2]
            print("[pre] wi.shape: ",wi.shape)
            print("[pre] xi.shape: ",xi.shape)
            wi = wi.view(wi.shape[0],wi.shape[1],-1)
            xi = xi.permute(0, 2, 3, 4, 1) # keep contiguous?
            xi = xi.view(xi.shape[0],-1,xi.shape[4])
            print("wi.shape: ",wi.shape)
            print("xi.shape: ",xi.shape)

            # -- compute cross-scale --
            score_map = torch.matmul(wi,xi) # q * v^T
            print("score_map.shape: ",score_map.shape)
            score_map = score_map.view(score_map.shape[0],score_map.shape[1],w,h)
            b_s, l_s, h_s, w_s = score_map.shape
            print("score_map.shape: ",score_map.shape)

            yi = score_map.view(b_s, l_s, -1)
            print("[1] yi.shape: ",yi.shape)
            yi = F.softmax(yi*self.softmax_scale, dim=2).view(l_s, -1)
            pi = pi.view(h_s * w_s, -1)
            print("pi.shape: ",pi.shape)
            yi = torch.mm(yi, pi)
            print("[2] yi.shape: ",yi.shape)
            print(self.stride_1)


            yi = yi.view(b_s, l_s, c_s, k_s, k_s)[0]
            zi = yi.view(1, l_s, -1).permute(0, 2, 1)
            zi = torch.nn.functional.fold(zi, (raw_int_bs[2], raw_int_bs[3]), (kernel, kernel), padding=paddings[0], stride=self.stride_1)
            inp = torch.ones_like(zi)
            inp_unf = torch.nn.functional.unfold(inp, (kernel, kernel), padding=paddings[0], stride=self.stride_1)
            out_mask = torch.nn.functional.fold(inp_unf, (raw_int_bs[2], raw_int_bs[3]), (kernel, kernel), padding=paddings[0], stride=self.stride_1)
            zi = zi / out_mask
            y.append(zi)

        y = torch.cat(y, dim=0)
        y = self.W(y)
        y = b + y

        if self.add_SE:
            y_SE=self.SE(y)
            y=self.conv33(torch.cat((y_SE*y,y),dim=1))

        print("y.shape: ",y.shape)
        return y

    def GSmap(self,a,b):
        return torch.matmul(a,b)

class SE_net(nn.Module):
    def __init__(self,in_channels,reduction=16):
        super(SE_net,self).__init__()
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.fc1=nn.Conv2d(in_channels=in_channels,out_channels=in_channels//reduction,kernel_size=1,stride=1,padding=0)
        self.fc2=nn.Conv2d(in_channels=in_channels//reduction,out_channels=in_channels,kernel_size=1,stride=1,padding=0)
    def forward(self, x):
        o1=self.pool(x)
        o1=F.relu(self.fc1(o1))
        o1=self.fc2(o1)
        return o1
class size_selector(nn.Module):
    def __init__(self,in_channels,intermediate_channels,out_channels):
        super(size_selector,self).__init__()
        self.embedding = nn.Sequential(
            nn.Linear(in_features=in_channels,out_features=intermediate_channels),
            nn.BatchNorm1d(intermediate_channels),
            nn.ReLU(inplace=True)
        )
        self.selector_a = nn.Linear(in_features=intermediate_channels,out_features=out_channels)
        self.selector_b = nn.Linear(in_features=intermediate_channels, out_features=out_channels)
        self.softmax = nn.Softmax(dim=1)
    def forward(self, x):
        vector = x.mean(-1).mean(-1)
        o1 = self.embedding(vector)
        a = self.selector_a(o1)
        b = self.selector_b(o1)
        v = torch.cat((a,b),dim=1)
        v = self.softmax(v)
        a = v[:,0,...,None,None,None]#.unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)
        b = v[:,1,...,None,None,None]#.unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)
        # a = v[:,0].unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)
        # b = v[:,1].unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)
        return a,b
