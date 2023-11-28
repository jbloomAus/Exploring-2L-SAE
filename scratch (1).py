# %%
import os
#os.environ["TRANSFORMERS_CACHE"] = "/workspace/cache/"
# %%
from neel.imports import *
from neel_plotly import *

SEED = 42
torch.manual_seed(SEED)
np.random.seed(SEED)
random.seed(SEED)
torch.set_grad_enabled(False)

model = HookedTransformer.from_pretrained("gelu-2l")

n_layers = model.cfg.n_layers
d_model = model.cfg.d_model
n_heads = model.cfg.n_heads
d_head = model.cfg.d_head
d_mlp = model.cfg.d_mlp
d_vocab = model.cfg.d_vocab
# %%
evals.sanity_check(model)
# %%
import transformer_lens
from transformer_lens import HookedTransformer, utils
import torch
import numpy as np
import gradio as gr
import pprint
import json
import torch.nn as nn
import torch.nn.functional as F
from datasets import load_dataset
from huggingface_hub import HfApi
from IPython.display import HTML
from functools import partial
import tqdm.notebook as tqdm
import plotly.express as px
import pandas as pd

# %%
DTYPES = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}
SAVE_DIR = Path("/workspace/1L-Sparse-Autoencoder/checkpoints")
class AutoEncoder(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        d_hidden = cfg["dict_size"]
        l1_coeff = cfg["l1_coeff"]
        dtype = DTYPES[cfg["enc_dtype"]]
        torch.manual_seed(cfg["seed"])
        self.W_enc = nn.Parameter(torch.nn.init.kaiming_uniform_(torch.empty(cfg["act_size"], d_hidden, dtype=dtype)))
        self.W_dec = nn.Parameter(torch.nn.init.kaiming_uniform_(torch.empty(d_hidden, cfg["act_size"], dtype=dtype)))
        self.b_enc = nn.Parameter(torch.zeros(d_hidden, dtype=dtype))
        self.b_dec = nn.Parameter(torch.zeros(cfg["act_size"], dtype=dtype))

        self.W_dec.data[:] = self.W_dec / self.W_dec.norm(dim=-1, keepdim=True)

        self.d_hidden = d_hidden
        self.l1_coeff = l1_coeff

        self.to(cfg["device"])
    
    def forward(self, x):
        x_cent = x - self.b_dec
        acts = F.relu(x_cent @ self.W_enc + self.b_enc)
        x_reconstruct = acts @ self.W_dec + self.b_dec
        l2_loss = (x_reconstruct.float() - x.float()).pow(2).sum(-1).mean(0)
        l1_loss = self.l1_coeff * (acts.float().abs().sum())
        loss = l2_loss + l1_loss
        return loss, x_reconstruct, acts, l2_loss, l1_loss
    
    @torch.no_grad()
    def make_decoder_weights_and_grad_unit_norm(self):
        W_dec_normed = self.W_dec / self.W_dec.norm(dim=-1, keepdim=True)
        W_dec_grad_proj = (self.W_dec.grad * W_dec_normed).sum(-1, keepdim=True) * W_dec_normed
        self.W_dec.grad -= W_dec_grad_proj
        # Bugfix(?) for ensuring W_dec retains unit norm, this was not there when I trained my original autoencoders.
        self.W_dec.data = W_dec_normed
    
    def get_version(self):
        version_list = [int(file.name.split(".")[0]) for file in list(SAVE_DIR.iterdir()) if "pt" in str(file)]
        if len(version_list):
            return 1+max(version_list)
        else:
            return 0

    def save(self):
        version = self.get_version()
        torch.save(self.state_dict(), SAVE_DIR/(str(version)+".pt"))
        with open(SAVE_DIR/(str(version)+"_cfg.json"), "w") as f:
            json.dump(cfg, f)
        print("Saved as version", version)
    
    @classmethod
    def load(cls, version):
        cfg = (json.load(open(SAVE_DIR/(str(version)+"_cfg.json"), "r")))
        pprint.pprint(cfg)
        self = cls(cfg=cfg)
        self.load_state_dict(torch.load(SAVE_DIR/(str(version)+".pt")))
        return self

    @classmethod
    def load_from_hf(cls, version, device_override=None):
        """
        Loads the saved autoencoder from HuggingFace. 
        
        Version is expected to be an int, or "run1" or "run2"

        version 25 is the final checkpoint of the first autoencoder run,
        version 47 is the final checkpoint of the second autoencoder run.
        """
        if version=="run1":
            version = 25
        elif version=="run2":
            version = 47
        
        cfg = utils.download_file_from_hf("NeelNanda/sparse_autoencoder", f"{version}_cfg.json")
        if device_override is not None:
            cfg["device"] = device_override

        pprint.pprint(cfg)
        self = cls(cfg=cfg)
        self.load_state_dict(utils.download_file_from_hf("NeelNanda/sparse_autoencoder", f"{version}.pt", force_is_torch=True))
        return self
encoder0 = AutoEncoder.load_from_hf("gelu-2l_L0_16384_mlp_out_51", "mps")
encoder1 = AutoEncoder.load_from_hf("gelu-2l_L1_16384_mlp_out_50", "mps")
# %%
data = load_dataset("NeelNanda/c4-10k", split="train")
tokenized_data = utils.tokenize_and_concatenate(data, model.tokenizer, max_length=128)
tokenized_data[0]

# %%
# SKIP FROM HERE ON

example_tokens = tokenized_data[:200]["tokens"]
logits, cache = model.run_with_cache(example_tokens)
per_token_loss = model.loss_fn(logits, example_tokens, True)
imshow(per_token_loss)
# %%
original_mlp_out = cache["mlp_out", 1]
loss, reconstr_mlp_out, hidden_acts, l2_loss, l1_loss = encoder1(original_mlp_out)
def reconstr_hook(mlp_out, hook, new_mlp_out):
    return new_mlp_out
def zero_abl_hook(mlp_out, hook):
    return torch.zeros_like(mlp_out)
print("reconstr", model.run_with_hooks(example_tokens, fwd_hooks=[(utils.get_act_name("mlp_out", 1), partial(reconstr_hook, new_mlp_out=reconstr_mlp_out))], return_type="loss"))
print("Orig", model(example_tokens, return_type="loss"))
print("Zero", model.run_with_hooks(example_tokens, return_type="loss", fwd_hooks=[(utils.get_act_name("mlp_out", 1), zero_abl_hook)]))
# %%

original_mlp_out = cache["mlp_out", 0]
loss, reconstr_mlp_out, hidden_acts, l2_loss, l1_loss = encoder0(original_mlp_out)
def reconstr_hook(mlp_out, hook, new_mlp_out):
    return new_mlp_out
def zero_abl_hook(mlp_out, hook):
    return torch.zeros_like(mlp_out)
print("reconstr", model.run_with_hooks(example_tokens, fwd_hooks=[(utils.get_act_name("mlp_out", 0), partial(reconstr_hook, new_mlp_out=reconstr_mlp_out))], return_type="loss"))
print("Orig", model(example_tokens, return_type="loss"))
print("Zero", model.run_with_hooks(example_tokens, return_type="loss", fwd_hooks=[(utils.get_act_name("mlp_out", 0), zero_abl_hook)]))
# %%
orig_logits = model(example_tokens)
orig_ptl = model.loss_fn(orig_logits, example_tokens, True)

zero_logits = model.run_with_hooks(example_tokens, return_type="logits", fwd_hooks=[(utils.get_act_name("mlp_out", 0), zero_abl_hook)])
zero_ptl = model.loss_fn(zero_logits, example_tokens, True)

recons_logits = model.run_with_hooks(example_tokens, fwd_hooks=[(utils.get_act_name("mlp_out", 0), partial(reconstr_hook, new_mlp_out=reconstr_mlp_out))], return_type="logits")
recons_ptl = model.loss_fn(recons_logits, example_tokens, True)
# %%
histogram(recons_ptl.flatten())
# %%
scatter(x=(recons_ptl-orig_ptl).flatten(), y=(zero_ptl-orig_ptl).flatten())
delta_ptl = recons_ptl - orig_ptl
histogram(delta_ptl.flatten(), marginal="box")
# %%
scipy.stats.kurtosis(to_numpy(delta_ptl).flatten())
# %%
token_df = nutils.make_token_df(example_tokens).query("pos>=1")
token_df["delta_ptl"] = to_numpy(delta_ptl.flatten())

# %%
display(token_df.sort_values("delta_ptl", ascending=False).head(20))
display(token_df.sort_values("delta_ptl", ascending=True).head(20))
# %%
virtual_weights = encoder0.W_dec @ model.W_in[1] @ model.W_out[1] @ encoder1.W_enc
virtual_weights.shape
# %%
histogram(virtual_weights.flatten()[::1001])
# %%
neuron2neuron = model.W_out[0] @ model.W_in[1]
histogram(neuron2neuron.flatten()[::101])
# %%
histogram(virtual_weights.mean(0), title="Ave by end feature")
histogram(virtual_weights.mean(1), title="Ave by start feature")
histogram(virtual_weights.median(0).values, title="Median by end feature")
histogram(virtual_weights.median(1).values, title="Median by start feature")
# %%

# START HERE! ====================

example_tokens = tokenized_data[:600]["tokens"]
_, cache = model.run_with_cache(example_tokens, stop_at_layer=2, names_filter=lambda x: "mlp_out" in x)
loss, recons_mlp_out0, hidden_acts0, l2_loss, l1_loss = encoder0(cache["mlp_out", 0])
loss, recons_mlp_out1, hidden_acts1, l2_loss, l1_loss = encoder1(cache["mlp_out", 1])


# %%
try:
    hidden_acts0 = hidden_acts0[:, 1:, :]
    hidden_acts0 = einops.rearrange(hidden_acts0, "batch pos d_enc -> (batch pos) d_enc")
    hidden_acts1 = hidden_acts1[:, 1:, :]
    hidden_acts1 = einops.rearrange(hidden_acts1, "batch pos d_enc -> (batch pos) d_enc")
except:
    print("FAILED")
    pass
hidden_is_pos0 = hidden_acts0 > 0
hidden_is_pos1 = hidden_acts1 > 0
d_enc = hidden_acts0.shape[-1]
cooccur_count = torch.zeros((d_enc, d_enc), device="mps", dtype=torch.float32)
for end_i in tqdm.trange(d_enc):
    cooccur_count[:, end_i] = hidden_is_pos0[hidden_is_pos1[:, end_i]].float().sum(0)
# %%
num_firings0 = hidden_is_pos0.sum(0)
num_firings1 = hidden_is_pos1.sum(0)
cooccur_freq = cooccur_count / torch.maximum(num_firings0[:, None], num_firings1[None, :])
# %%
# cooccur_count = cooccur_count.float() / hidden_acts0.shape[0]
# %%
histogram(cooccur_freq[cooccur_freq>0.1], log_y=True)
# %%
cooccur_freq[cooccur_freq.isnan()] = 0.
val, ind = cooccur_freq.flatten().topk(10)

start_topk_ind = (ind // d_enc)
end_topk_ind = (ind % d_enc)
print(val)
print(start_topk_ind)
print(end_topk_ind)

print(num_firings0[start_topk_ind])
print(num_firings1[end_topk_ind])
# # %%
# cooccur_freq.topk(10)
# # %%
# cooccur_df = pd.DataFrame({
#     "start": [i for i in range(d_enc) for j in range(d_enc)],
#     "end": [j for i in range(d_enc) for j in range(d_enc)],
#     "cooccur_freq": to_numpy(cooccur_freq.flatten()),
# })
# cooccur_df.sort_values("cooccur_freq", ascending=False).head(20)
# # %%

# print(num_firings0[[1775,  9818,  4323,  1997, 11097,  9818, 11097,  4323,  1775,  1997]])
# print(num_firings1[[9835, 14644]])
# # %%

# %%
feature_id = 2593
layer = 0
token_df = nutils.make_token_df(example_tokens).query("pos>=1")
token_df["val"] = to_numpy(hidden_acts1[:, feature_id])
pd.set_option('display.max_rows', 50)
display(token_df.sort_values("val", ascending=False).head(50))

# %%
logit_weights = encoder1.W_dec[feature_id, :] @ model.W_U
vocab_df = nutils.create_vocab_df(logit_weights)
vocab_df["has_space"] = vocab_df["token"].apply(lambda x: nutils.SPACE in x)
px.histogram(vocab_df, x="logit", color="has_space", barmode="overlay", marginal="box")
display(vocab_df.head(20))
# %%
is_an = example_tokens[:, 1:].flatten() == model.to_single_token(" an")

# %%
line(hidden_is_pos0[is_an].float().mean(0))
line(hidden_is_pos1[is_an].float().mean(0))
# %%
scatter(x=virtual_weights[5740], y=hidden_is_pos1[is_an].float().mean(0), hover=np.arange(d_enc))
# %%
l0_feature_an = 5740
line(hidden_acts0[5740])
replacement_for_feature = torch.zeros_like(example_tokens).cuda()
replacement_for_feature[:, 1:] = hidden_acts0[:, 5740].reshape(600, 127)
mlp_out0_diff = replacement_for_feature[:, :, None] * encoder0.W_dec[l0_feature_an]

new_end_topk = end_topk_ind[start_topk_ind==5740]

def remove_an_feature(mlp_out, hook):
    mlp_out[:, :] -= mlp_out0_diff
    return mlp_out
model.reset_hooks()
model.blocks[0].hook_mlp_out.add_hook(remove_an_feature)
_, new_cache = model.run_with_cache(example_tokens, stop_at_layer=2, names_filter=lambda x: "mlp_out" in x)
loss, x_reconstruct, hidden_acts_new, l2_loss, l1_loss = encoder1(new_cache["mlp_out", 1])
model.reset_hooks()
# %%
new_hidden_acts_on_an = hidden_acts_new[:, :, new_end_topk].reshape(-1, 5)[example_tokens.flatten()==model.to_single_token(" an"), :]
old_hidden_acts_on_an = hidden_acts1[:, new_end_topk][example_tokens[:, 1:].flatten()==model.to_single_token(" an"), :]
for i in range(5):
    scatter(x=old_hidden_acts_on_an[:, i], y=new_hidden_acts_on_an[:, i], hover=np.arange(180), title=new_end_topk[i].item(), include_diag=True, yaxis="POst Ablation", xaxis="Pre Ablation")
# %%

# 28/11/2023 ====================

def get_feature_acts(point, layer, sae, num_batches = 1000, minibatch_size = 50):
  try:
    del feature_acts
    del random_feature_acts
  except NameError:
    pass

  # get however many tokens we need
  toks = tokenized_data["tokens"][:num_batches]
  toks = toks.to("mps")

  # get activations on test tokens at point of interest. Run model on batches of tokens with size [batch_size, 128]. Be careful with RAM.

  for i in tqdm.tqdm(range(toks.size(0)//minibatch_size)):
    # split toks into minibatch and run model with cache on minibatch
    toks_batch = toks[minibatch_size*i : minibatch_size*(i+1), :]
    logits, cache = model.run_with_cache(toks_batch, stop_at_layer=layer+1, names_filter=utils.get_act_name(point, layer))
    del logits

    act_batch = cache[point, layer]
    act_batch = act_batch.detach().to('mps')
    del cache

    # get feature acts on this minibatch (fewer random ones to save RAM)
    feature_act_batch = torch.relu(einops.einsum(act_batch - sae.b_dec, sae.W_enc, "batch seq resid , resid mlp -> batch seq mlp")  + sae.b_enc)

    del act_batch

    # append minibatch feature acts to storage variable
    if i == 0:  # on first iteration, create feature_acts
      feature_acts = feature_act_batch
    else:  # then add to it
      feature_acts = torch.cat([feature_acts, feature_act_batch], dim=0)

    del feature_act_batch

  # set BOS acts to zero
  feature_acts[:, 0, :] = 0

  # flatten [batch n_seq] dimensions
  #feature_acts = feature_acts.reshape(-1, feature_acts.size(2))

  print("feature_acts has size:", feature_acts.size())

  return toks, feature_acts

# %%

toks, acts = get_feature_acts("mlp_out", 0, encoder0, num_batches=1024, minibatch_size=64)
# %%

# iterate through acts to get co occurring features

def compute_cooccurrences(hidden_acts):
    try:
        hidden_acts = hidden_acts[:, 1:, :]
        hidden_acts = einops.rearrange(hidden_acts, "batch pos d_enc -> (batch pos) d_enc")
    except Exception as e:
        print("FAILED:", e)
        return None

    hidden_is_pos = hidden_acts > 0
    d_enc = hidden_acts.shape[-1]

    cooccur_count = torch.zeros((d_enc, d_enc), device="cpu", dtype=torch.float32)
    for end_i in tqdm.trange(d_enc):
        cooccur_count[:, end_i] = hidden_is_pos[hidden_is_pos[:, end_i]].float().sum(0)

    num_firings = hidden_is_pos.sum(0).to("cpu")
    cooccur_freq = cooccur_count / torch.maximum(num_firings[:, None], num_firings[None, :])
    cooccur_freq[cooccur_freq.isnan()] = 0.

    cooccur_freq.fill_diagonal_(0.)

    return cooccur_freq
# %%

# Usage example
# hidden_acts is your input tensor
cooccur_table = compute_cooccurrences(acts)
print(cooccur_table)

# %%
print(cooccur_table.shape)
# %%
df = pd.DataFrame(cooccur_table.numpy())
# %%
# turn df into a long form dataframe
df = df.stack().reset_index()

# %%
df[0].describe()
# %%
(cooccur_table.flatten()>.7).sum()
# %%
mean_acts = (acts > 0).float().mean(dim=(0,1))
print(mean_acts.shape)
# %%
d_enc = mean_acts.shape[-1]
df["mean_act_feature1"] = mean_acts.detach().cpu().repeat(d_enc)
df["mean_act_feature2"] = mean_acts.detach().cpu().repeat(1, d_enc).flatten()
# %%
mean_acts.shape
# %%
df.head()
# %%
df_sparse = df[(df['mean_act_feature1'] < 1e-5) & (df['mean_act_feature2'] < 1e-5)]
df_sparse.shape
# %%
df.shape
# %%
acts.shape

# %%
df_sparse[df_sparse[0] > 0].sort_values(0)
# %%
