import numpy as np
import os
import pandas as pd
import random
import torch
import torch.nn.functional as F
import traceback

from collections import defaultdict
from datasets import load_dataset
from hdbscan import HDBSCAN, validity_index
from itertools import product

from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from sklearn.metrics import silhouette_score
from tqdm import tqdm
from transformers import AutoConfig, AutoModel, AutoTokenizer

"""
Run various experiments on transformer models.

We use the following shorthand notation in comments:

N: number of tokens
d: dimensionality of token space
d': dimensionality of query/key space
L: number of layers
S: sample size
"""

RANDOM_SEED = 42


def seed_everything(seed):
    """
    credit goes to https://gist.github.com/ihoromi4/b681a9088f348942b01711f251e5f964
    """
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True


def stack_padded(ndarray_list, pad_value=0):
    assert ndarray_list  # not empty
    target_shape = [len(ndarray_list),
                    *[max(a.shape[dim] for a in ndarray_list) for dim in range(ndarray_list[0].ndim)]]
    stacked_array = np.full(target_shape, fill_value=pad_value, dtype=ndarray_list[0].dtype)
    for i, a in enumerate(ndarray_list):
        stacked_array[i, *[slice(0, a.shape[dim]) for dim in range(a.ndim)]] = a
    return stacked_array


def build_model(model_id, random_params, no_dense_layers, num_hidden_layers):
    config = AutoConfig.from_pretrained(model_id)
    if num_hidden_layers:  # override depth (only works for models with shared params)
        config.num_hidden_layers = num_hidden_layers
    if random_params:  # initialise params randomly and use single head
        config.num_attention_heads = 1
        model = AutoModel.from_config(config)
    else:  # get learnt params
        model = AutoModel.from_pretrained(model_id, config=config)
    if no_dense_layers:  # set dense layer params to zero
        disable_dense_layers(model)
    return model


def disable_dense_layers(model):
    model_type = model.config.model_type
    match model_type:
        case "albert":
            with torch.no_grad():
                model.encoder.albert_layer_groups[0].albert_layers[0].attention.dense.weight.fill_(0.0)
                model.encoder.albert_layer_groups[0].albert_layers[0].attention.dense.bias.fill_(0.0)
        case "bert":
            for i in range(len(model.encoder.layer)):
                with torch.no_grad():
                    model.encoder.layer[i].attention.output.dense.weight.fill_(0.0)
                    model.encoder.layer[i].attention.output.dense.bias.fill_(0.0)


def extract_queries_and_keys(model, head):
    num_layers = model.config.num_hidden_layers
    head_ndim = model.config.hidden_size // model.config.num_attention_heads
    model_type = model.config.model_type
    head_extractor = lambda layer: (lambda x: layer(x)[..., (head * head_ndim):((head + 1) * head_ndim)])
    match model_type:
        case "albert":
            shared_query = head_extractor(model.encoder.albert_layer_groups[0].albert_layers[0].attention.query)
            shared_key = head_extractor(model.encoder.albert_layer_groups[0].albert_layers[0].attention.key)
            return num_layers * [shared_query], num_layers * [shared_key]
        case "bert":
            queries = [head_extractor(l.attention.self.query) for l in model.encoder.layer]
            keys = [head_extractor(l.attention.self.key) for l in model.encoder.layer]
            return queries, keys
        case _:
            raise NotImplementedError("Unsupported model type", model_type)


def make_outdir(model, dataset, random_params, no_dense_layers):
    run_id = "uniform" if dataset is None else dataset.info.dataset_name
    run_id += f"_{model.config._name_or_path}"
    run_id += f"_params-{'random' if random_params else 'trained'}"
    run_id += f"_dense-{'off' if no_dense_layers else 'on'}"
    run_id += f"_layers-{model.config.num_hidden_layers}"
    outdir = f"rawsults/{run_id}"
    os.makedirs(outdir, exist_ok=True)
    return outdir


def get_random_input(dataset, tokeniser):
    MIN_NUM_TOKENS = 300
    while True:
        if dataset is not None:
            index = torch.randint(low=0, high=len(dataset), size=(1,)).item()
            original_text = dataset[index]["text"]
        else:
            original_text = tokeniser.decode(torch.randint(tokeniser.vocab_size, (tokeniser.model_max_length,)))
        input = tokeniser(original_text, return_tensors="pt", truncation=True, return_attention_mask=False)
        if input.input_ids.shape[1] >= MIN_NUM_TOKENS:
            break
    tokenised_text = "(" + ")(".join(tokeniser.batch_decode(input.input_ids.squeeze())) + ")"
    return input, original_text, tokenised_text


def compute_cos_similarities(hidden_states):
    sim_matrices = []
    for X in hidden_states:
        X = torch.from_numpy(X)  # shape: N x d
        X_unit = F.normalize(X, dim=-1)  # normalise each row
        sim_matrix = torch.matmul(X_unit, X_unit.transpose(0, 1))  # shape: N x N
        sim_matrices.append(sim_matrix.numpy(force=True))
    return sim_matrices


def compute_attention_logits(hidden_states, queries, keys):
    logit_matrices = []
    for X, query, key in zip(hidden_states, queries, keys):
        X = torch.from_numpy(X)  # shape: N x d
        Q, K = query(X), key(X)  # shape: N x d'
        factor = K.shape[-1] ** -0.5  # for numerical stability
        logit_matrix = factor * torch.matmul(Q, K.transpose(0, 1))  # shape: N x N
        logit_matrices.append(logit_matrix.numpy(force=True))
    return logit_matrices


def compute_clustering(data):
    labels = []
    metrics = []
    for i, X in enumerate(data):
        X = X.astype(np.float64)  # cast to 64-bit floats to avoid numerical issues
        X = PCA(n_components=32, random_state=RANDOM_SEED).fit_transform(X)
        clustering = HDBSCAN(min_cluster_size=3).fit(X)
        num_clusters = clustering.labels_.max() + 1
        outlier_rate = (clustering.labels_ == -1).sum() / len(clustering.labels_)
        silh_score = silhouette_score(X, clustering.labels_) if num_clusters > 1 else 1.0
        dbcv_score = validity_index(X, clustering.labels_) if num_clusters > 1 else 1.0
        labels.append(clustering.labels_)
        metrics.append(np.array([num_clusters, outlier_rate, silh_score, dbcv_score]))
    return labels, metrics


def compute_tsne_embeddings(data):
    return [TSNE(n_components=2, perplexity=5).fit_transform(X) for X in data]


def run_experiment(dataset, model_id, random_params=False, no_dense_layers=False,
                   num_hidden_layers=None, sample_size=10, num_bins=100):
    """
    This function produces artifacts in the form of .npy files on the disk with the following shapes:

    "(token_similarity|attention_logits).npy": S x L x N x N
    "(token|attention_logit)_cluster_metrics.npy": S x L x 4
    "(token|attention_logit)_cluster_labels.npy": S x L x N
    "(token|attention_logit)_t-SNE_embeddings.npy": S x L x N x 2
    """
    seed_everything(RANDOM_SEED)  # ensure reproducibility

    model = build_model(model_id, random_params, no_dense_layers, num_hidden_layers)
    tokeniser = AutoTokenizer.from_pretrained(model_id)
    outdir = make_outdir(model, dataset, random_params, no_dense_layers)

    overview_df = pd.DataFrame(columns=["original_text", "tokenised_text", "num_tokens"])
    results = defaultdict(list)  # non-existent keys are initialised with empty list

    run_pbar = tqdm(range(sample_size), desc="Processing each sample")
    for sample_idx in run_pbar:
        # input from dataset
        input, original_text, tokenised_text = get_random_input(dataset, tokeniser)
        overview_df.loc[sample_idx] = (original_text, tokenised_text, input.input_ids.numel())

        # output from model
        output = model(**input, output_hidden_states=True)
        hidden_states = [X.squeeze(0).numpy(force=True)  # shape: N x d
                         for X in output.hidden_states]  # ignore input layer

        # token similarities
        run_pbar.set_postfix_str("dotprods")
        sim_matrices = compute_cos_similarities(hidden_states[1:])
        results[f"token_similarity"].append(np.stack(sim_matrices))  # shape: L x N x N

        # attention logits
        head = random.choice(range(model.config.num_attention_heads))  # choose random attention head
        queries, keys = extract_queries_and_keys(model, head)
        logit_matrices = compute_attention_logits(hidden_states[:-1], queries, keys)
        results[f"attention_logits"].append(np.stack(logit_matrices))  # shape: L x N x N

        # clustering and t-SNE
        run_pbar.set_postfix_str("clustering")
        for target in ["token", "attention_logit"]:
            data = hidden_states[1:] if target == "X" else logit_matrices
            labels, metrics = compute_clustering(data)
            tsnes = compute_tsne_embeddings(data)
            results[f"{target}_cluster_labels"].append(np.stack(labels))  # shape: L x N
            results[f"{target}_cluster_metrics"].append(np.stack(metrics))  # shape: L x 4
            results[f"{target}_t-SNE_embeddings"].append(np.stack(tsnes))  # shape: L x N x 2

    overview_df.to_csv(f"{outdir}/0verview.csv", index_label="sample_idx")
    for key, val in results.items():
        if val:  # ignore empty lists
            np.save(f"{outdir}/{key}.npy", stack_padded(val))  # shape: S x previous


if __name__ == "__main__":
    print("Loading datasets...")
    wikitext = load_dataset("wikitext", "wikitext-103-v1", split="all")
    imdb = load_dataset("stanfordnlp/imdb", split="all")

    print("Running experiments...")
    combinations = list(product([None, wikitext, imdb],
                                ["albert-xlarge-v2"], #["albert-large-v2", "bert-large-uncased"],
                                [False, True],
                                [False, True]))
    for i, (dataset, model_id, random_params, no_dense_layers) in enumerate(combinations):
        print(f"EXPERIMENT {i + 1}/{len(combinations)}")
        try:
            run_experiment(dataset,
                           model_id,
                           random_params,
                           no_dense_layers,
                           num_hidden_layers=48 if model_id.startswith("albert") else None)
        except Exception as e:
            print("FAILED:", repr(e))
            print(traceback.format_exc())
