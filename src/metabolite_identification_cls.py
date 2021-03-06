####
#
# The MIT License (MIT)
#
# Copyright 2018 Eric Bach <eric.bach@aalto.fi>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is furnished
# to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
####

'''

Functions related to the metabolite identification experiments:

    - Modelling of the layered graph containing the candidate information.
    - Dynamic programming approach for the shortest path algorithm.
    - Reranking of the molecular candidates by score itegration.

'''

import numpy as np
import time
import os
import re
import copy

## my own classes, e.g. ranksvm, retention graph, etc ...
from helper_cls import join_dicts
from helper_cls import  is_sorted
from rank_svm_cls import load_data, KernelRankSVC
# load my own kernels
from rank_svm_cls import tanimoto_kernel, tanimoto_kernel_mat, minmax_kernel_mat, minmax_kernel
# load function for the model selection
from model_selection_cls import find_hparan_ranksvm

## scikit-learn methods
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import  StandardScaler, MinMaxScaler, Normalizer, PolynomialFeatures
from sklearn.metrics.pairwise import pairwise_kernels

## Data structures
from pandas import DataFrame
from collections import OrderedDict

## Allow the paralellization of the candidate graph construction
from joblib import Parallel, delayed

def train_model_using_all_data (
        training_systems, predictor, pair_params, kernel_params, opt_params, input_dir, estimator,
        feature_type, n_jobs = 1):
    """
    Taks: Train a RankSVM model that uses all provided data for training. The hyper-parameter
          are chosen using cross-validation on that training set.

    :param training_systems: list of strings, containing the training systems

    :param predictor: list of string, containing the predictors / molecular features used for the
        model construction.

    :param pair_params: dictionary, containing the paramters used for the creation of
        the RankSVM learning pairs, e.g. minimum and maximum oder distance.

    :param kernel_params: dictionary, containing the parameters for the kernels and
        generally for handling the input features / predictors. See definition of the
        dictionary in the __main__ of file 'evaluation_scenario_cls.py'.

    :param opt_params: dictionary, containing the paramters controlling the hyper-paramter
        optimization, number of cross-validation splits, etc. See definition of the
        dictionary in the __main__ of file 'evaluation_scenario_cls.py'.

    :param input_dir: string, directory containing the input data, e.g., fingerprints and retention
        times.

    :param estimator: string, order predictor to use: either "ranksvm" or "svr".

    :param feature_type: string, feature type that is used for the RankSVM. Currently
        only 'difference' features are supported, i.e., \phi_j - \phi_i is used for
        the decision. If the estimator is not RankSVM, but e.g. Support Vector Regression,
        than tis parameter can be set to None and is ignored.

    :param n_jobs: integer, number of jobs used for the hyper-parameter estimation. The maximum number
        of used jobs, is the number of inner splits (cross-validation or random split)!

    :return: tuple

        1) ranking_model: KernelRankSVC estimator object
        2) best_params: dictionary, containing combination of best parameters
            E.g.: {"C": 1, "gamma": 0.25}
    """

    # Number of cv-splits used for the parameters estimated
    n_splits_cv = opt_params["n_splits_ncv"]

    # Slack type for RankSVM
    slack_type = opt_params["slack_type"]

    # See 'evaluate_on_target_systems' for description:
    all_pairs_for_test = opt_params["all_pairs_for_test"]

    if estimator != "ranksvm":
        raise ValueError ("Unsupported estimator: %s" % estimator)

    # RankSVM regularization parameter
    param_grid = {"C": opt_params["C"]}

    if kernel_params["kernel"] == "linear":
        kernel = "linear"
    elif kernel_params["kernel"] in ["rbf", "gaussian"]:
        param_grid["gamma"] = kernel_params["gamma"]
        kernel = "rbf"
    elif kernel_params["kernel"] == "tanimoto":
        if estimator in ["ranksvm", "kernelridge"]:
            kernel = tanimoto_kernel
        elif estimator in ["svr"]:
            kernel = tanimoto_kernel_mat
    elif kernel_params["kernel"] == "minmax":
        if estimator in ["ranksvm", "kernelridge"]:
            kernel = minmax_kernel
        elif estimator in ["svr"]:
            kernel = minmax_kernel_mat
    else:
        raise ValueError ("Invalid kernel: %s." % kernel_params["kernel"])

    if isinstance (training_systems, str):
        training_systems = [training_systems]

    n_training_systems = len (training_systems)
    print ("Training systems (# = %d): %s" % (n_training_systems, ",".join (training_systems)))

    ## Load the target and training systems into directories using (molecule, system)-keys
    ## and retention times respectively molecular features as values

    # If we use molecular descriptors, we need to scale the data, e.g. to [0, 1].
    if kernel_params["scaler"] == "noscaling":
        scaler = None
    elif kernel_params["scaler"] == "minmax":
        scaler = MinMaxScaler()
    elif kernel_params["scaler"] == "std":
        scaler = StandardScaler()
    elif kernel_params["scaler"] == "l2norm":
        scaler = Normalizer()
    else:
        raise ValueError ("Invalid scaler for the molecular features: %s"
                          % kernel_params["scaler"])

    # Handle MACCS counting fingerprints
    if predictor[0] == "maccsCount_f2dcf0b3":
        predictor_c = ["maccs"]
        predictor_fn = "fps_maccs_count.csv"
    else:
        predictor_c = predictor
        predictor_fn = None

    d_rts, d_features, d_system_index = OrderedDict(), OrderedDict(), OrderedDict()
    for k_sys, system in enumerate (training_systems):
        rts, data = load_data (input_dir, system = system, predictor = predictor_c, pred_fn = predictor_fn)

        # Use (mol-id, system)-tupel as key
        keys = list (zip (rts.inchi.values, [system] * rts.shape[0]))

        # Values: retention time, features
        rts  = rts.rt.values.reshape (-1, 1)
        data = data.drop ("inchi", axis = 1).values

        if kernel_params["poly_feature_exp"]:
            # If we use binary fingerprints, we can include some
            # interactions, e.g. x_1x_2, ...
            data = PolynomialFeatures (interaction_only = True, include_bias = False).fit_transform (data)

        # Make ordered directories
        d_rts[system], d_features[system] = OrderedDict(), OrderedDict()

        for i, key in enumerate (keys):
            d_rts[system][key] = rts[i, 0]
            d_features[system][key] = data[i, :]

        # Dictionary containing a unique numeric identifier for each system
        d_system_index[system] = k_sys

        if scaler is not None:
            if getattr (scaler, "partial_fit", None) is not None:
                # 'partial_fit' allows us to learn the parameters of the scaler
                # online. (great stuff :))
                scaler.partial_fit (data)
            else:
                # We have scaler at hand, that does not allow online fitting.
                # This probably means, that this is a scaler, that performs
                # the desired scaling for each example independently, e.g.
                # sklearn.preprocessing.Normalizer.
                pass

    for system in training_systems:
        print ("Training set '%s' contains %d examples." % (system, len (d_rts[system])))

    # Collect all the data that is available for training.
    d_rts_training = join_dicts (d_rts)
    d_features_training = join_dicts (d_features)

    # Train the model
    start_time = time.time()

    best_params, cv_results, n_train_pairs, ranking_model, _, _ = find_hparan_ranksvm (
        estimator = KernelRankSVC (kernel = kernel, slack_type = slack_type, random_state = 319),
        fold_score_aggregation = "weighted_average", X = d_features_training, y = d_rts_training,
        param_grid = param_grid, cv = GroupKFold (n_splits = n_splits_cv), pair_params = pair_params,
        n_jobs = n_jobs, scaler = scaler, all_pairs_as_test = all_pairs_for_test)

    rtime_gcv = time.time() - start_time
    print ("[find_hparam_*] %.3fsec" % rtime_gcv)
    print (DataFrame (cv_results))

    return ranking_model, best_params

def build_candidate_structure (
        model, input_dir_candidates, n_jobs = 1, verbose = False):
    """
    Building up the structure collection the information about the molecular candidates.
    The candidate information are represented as list if dictionaries, with one dictionary
    per layer. Compare Figure 3 and Section 2.3.2 in the paper.

    :param model: dictionary, containing the order prediction model

        "ranking_model": Currently only _fitted_ (see .fit(...)) KernelRankSVC objects are supported
        "predictor": list of strings, containing the predictors used to train the model.
                     Currently only 'maccs' and 'maccsCount_f2dcf0b3' are supported.

    :param input_dir_candidates: string, directory containing the scoring and
        fingerprints of the candidates with following structure:
        E.g.: input_dir_candidates = "./"
            ./candidates/
            |
            --> scorings/
            |   |
            |   --> maccs_binary/: scoring files, with candidates for which binary maccs fps
            |   |                  could be calculated
            |   |
            |   --> maccs_count/: scoring files, with candidates for which counting maccs fps
            |   |                 could be calculated
            |   |
            |   --> before_cleaning_up/: scoring files, with all candidates corresponding to the
            |                            MSMS-spectra
            |
            --> fingerprints/
            |   |
            |   --> maccs_binary/: maccs binary fingerprints of the candidates for each
            |   |                  MSMS-spectrum separetly
            |   |
            |   --> maccs_count/: maccs count fingerprints of the candidates for each MSMS-sepctrum
            |                     separetly
            |
            --> rts_msms.csv: File containing the molecular structure and corresponding retention time
                              for each MSMS-spectrum in the dataset:
                              E.g.:
                                "inchikey","inchi","rt"
                                "AAOVKJBEBIDNHE-UHFFFAOYSA-N","InChI=1S/C16H13ClN2O/c1-19...",9.3
                                "ACWBQPMHZXGDFX-QFIPXVFZSA-N","InChI=1S/C24H29N5O3/c1-4-5-10...",8.9

    :param n_jobs: scaler, number of jobs used. The candidate structure is build layer by layer,
        i.e., the parallelization can be performed by processing each layer separetly.
        (default = 1)

    :param verbose: boolean, should the Parallel function be verbose? (default = False)

    :return: list of dictionaries, for their structure check '_candidate_iokrscores_and_rankingscores'.
    """
    # Read the retention times for each MSMS-spectrum and sort them in increasing order
    msms_rts = DataFrame.from_csv (input_dir_candidates + "/rts_msms.csv", index_col = None).sort_values ("rt")

    # Load the map from inchikey --> inchi in order to find the correct candidates
    # in the scoring list.
    d_inchikey2inchi = {inchikey: inchi for inchikey, inchi in msms_rts[["inchikey", "inchi"]].values}

    # Create list of candidate dictionaries
    l_data = Parallel (n_jobs = n_jobs, verbose = verbose)(
        delayed (_candidate_iokrscores_and_rankingscores)(
            spec = spec_id, layer = spec_idx, input_dir_candidates = input_dir_candidates,
            model = model, msms_rts = msms_rts, d_inchikey2inchi = d_inchikey2inchi)
        for spec_idx, spec_id in enumerate (msms_rts.inchikey.values))

    return l_data

def shortest_path (cand_data, weight_fun, cut_off_n_cand = np.inf, check_input = False, **kwds):
    """
    Shortest path algorithm to find best metabolite assignment. See Algorithm 1 in the paper.

    :param cand_data: list of dictionaries, containing the information about the
        candidate in each layer (see 'build_candidate_structure')

    :param weight_function: function, weight function to calculate the edge weights
        in the candidate graph (compare Figure 3).

    :param cut_off_n_cand: intenger, maximum number of candidates considered in each
        layer. E.g. if set to 300 than only the 300 molecular candidates with the
        highest msms-based scores are used to find the shortest path.

    :param check_input: boolean, perform some consistency checks on the input candidate structure.

    :param kwds: dictionary, parameters passed to the weight function

    :return: (nodes_shortest_path, len_shortest_path)-tuple

        nodes_shortest_path: list of integers, node ids along the shortest path
        len_shortest_path: scalar, length of the shortest path
    """
    kwds_c = copy.deepcopy (kwds)

    n_layer = len (cand_data)

    if check_input:
        rt = -np.inf
        for t in range (n_layer):
            assert (is_sorted (cand_data[t]["iokrscores"], ascending = False))
            assert (cand_data[t]["rt_cand_list"] >= rt)
            rt = cand_data[t]["rt_cand_list"]

    # List of lists to store the accumulated lengths of the paths
    n_cand_first = len (cand_data[0]["iokrscores"])

    # We need to take the scores of the first layer into account!
    S_path = [[- score for score in cand_data[0]["iokrscores"]]]
    dW_path = [[0] * n_cand_first]
    parents = [[-1] * n_cand_first]

    # We need to pass the RankSVM score differences to the weighting function
    kwds_c["dW_path"] = dW_path

    # Dynamically update the path scores
    for t in range (n_layer - 1):
        tp1 = t + 1

        n_cand_t = int (np.minimum (len (cand_data[t]["iokrscores"]), cut_off_n_cand))
        n_cand_tp1 = int (np.minimum (len (cand_data[tp1]["iokrscores"]), cut_off_n_cand))

        S_path.append ([np.inf] * n_cand_tp1)
        dW_path.append ([-np.inf] * n_cand_tp1)
        parents.append ([np.nan] * n_cand_tp1)

        for i in range (n_cand_t):
            # Path length in (t,i)
            s_t_i = S_path[t][i]
            # Sum of pairwise predicitons: sum_{(t-1,k)<(t,i)} w^T(\phi_(t,i)-\phi_(t-1,k))
            dW_t_i = dW_path[t][i]

            for j in range (n_cand_tp1):
                s_update, dw_tp1_j = weight_fun (cand_data, (t, i), (tp1, j), **kwds_c)

                # Path length in (t+1,j)
                s_tp1_j = s_t_i + s_update

                if S_path[tp1][j] > s_tp1_j:
                    S_path[tp1][j] = s_tp1_j
                    dW_path[tp1][j] = dW_t_i + dw_tp1_j
                    parents[tp1][j] = i

    # Find shortest path and its length
    # - Starting node (backwards) is the one that corresponds to the
    #   end-point of the shortest path.
    nodes_shortest_path = [np.argmin (S_path[-1])]
    # - The lenght of the shortest path is the score in this node.
    len_shortest_path = S_path[-1][nodes_shortest_path[0]]

    t = n_layer - 1
    while True:
        par = parents[t][nodes_shortest_path[-1]]
        if par == -1:
            break
        nodes_shortest_path.append (par)
        t -= 1

    nodes_shortest_path.reverse()

    return nodes_shortest_path, len_shortest_path

def shortest_path_exclude_candidates (
        cand_data, weight_fun, cut_off_n_cand = np.inf, check_input = False,
        exclude_blocked_candidates = False, **kwds):
    """
    Shortest path algorithm to find best metabolite assignment. See Algorithm 1 in the paper.

    Small modification: Molecular candidates can be blocked and than excluded from a shortest path.
                        This allows the extraction of several shortest paths. In the paper we only
                        look at the first shortests path and therefore the results are equivalent
                        with the 'shortest_path' function.

    :param cand_data: list of dictionaries, containing the information about the
        candidate in each layer (see 'build_candidate_structure')

    :param weight_function: function, weight function to calculate the edge weights
        in the candidate graph (compare Figure 3).

    :param cut_off_n_cand: intenger, maximum number of candidates considered in each
        layer. E.g. if set to 300 than only the 300 molecular candidates with the
        highest msms-based scores are used to find the shortest path.

    :param check_input: boolean, perform some consistency checks on the input candidate structure.

    :param exclude_blocked_candidates: boolean, should blocked candidates be excluded?

    :param kwds: dictionary, parameters passed to the weight function

    :return: (nodes_shortest_path, len_shortest_path)-tuple

        nodes_shortest_path: list of integers, node ids along the shortest path
        len_shortest_path: scalar, length of the shortest path
    """
    kwds_c = copy.deepcopy (kwds)

    n_layer = len (cand_data)

    if check_input:
        rt = -np.inf
        for t in range (n_layer):
            assert (is_sorted (cand_data[t]["iokrscores"], ascending = False))
            assert (cand_data[t]["rt_cand_list"] >= rt)
            rt = cand_data[t]["rt_cand_list"]

            if exclude_blocked_candidates:
                assert ("is_blocked" in cand_data[t].keys())
                assert (len (cand_data[t]["is_blocked"]) == len (cand_data[t]["iokrscores"]))

    # List of lists to store the accumulated lengths of the paths
    n_cand_first = len (cand_data[0]["iokrscores"])

    # We need to take the scores of the first layer into account!
    S_path = [[- score for score in cand_data[0]["iokrscores"]]]
    dW_path = [[0] * n_cand_first]
    parents = [[-1] * n_cand_first]

    # We need to pass the RankSVM score differences to the weighting function
    kwds_c["dW_path"] = dW_path

    # Dynamically update the path scores
    for t in range (n_layer - 1):
        tp1 = t + 1

        n_cand_t = int (np.minimum (len (cand_data[t]["iokrscores"]), cut_off_n_cand))
        n_cand_tp1 = int (np.minimum (len (cand_data[tp1]["iokrscores"]), cut_off_n_cand))

        S_path.append ([np.inf] * n_cand_tp1)
        dW_path.append ([-np.inf] * n_cand_tp1)
        parents.append ([np.nan] * n_cand_tp1)

        for i in range (n_cand_t):
            if exclude_blocked_candidates and cand_data[t]["is_blocked"][i]:
                continue

            # Path length in (t,i)
            s_t_i = S_path[t][i]
            # Sum of pairwise predicitons: sum_{(t-1,k)<(t,i)} w^T(\phi_(t,i)-\phi_(t-1,k))
            dW_t_i = dW_path[t][i]

            for j in range (n_cand_tp1):
                if exclude_blocked_candidates and cand_data[tp1]["is_blocked"][j]:
                    continue

                s_update, dw_tp1_j = weight_fun (cand_data, (t, i), (tp1, j), **kwds_c)

                # Path length in (t+1,j)
                s_tp1_j = s_t_i + s_update

                if S_path[tp1][j] > s_tp1_j:
                    S_path[tp1][j] = s_tp1_j
                    dW_path[tp1][j] = dW_t_i + dw_tp1_j
                    parents[tp1][j] = i

    # Find shortest path and its length
    # - Starting node (backwards) is the one that corresponds to the
    #   end-point of the shortest path.
    nodes_shortest_path = [np.argmin (S_path[-1])]
    # - The lenght of the shortest path is the score in this node.
    len_shortest_path = S_path[-1][nodes_shortest_path[0]]

    t = n_layer - 1
    while True:
        par = parents[t][nodes_shortest_path[-1]]
        if par == -1:
            break
        nodes_shortest_path.append (par)
        t -= 1

    nodes_shortest_path.reverse()

    return nodes_shortest_path, len_shortest_path

def _weight_func_max (cand_data, u, v, **kwargs):
    """
    Task: Calculate the edge-weight for a graph during the shortest path
          algorithm. The weight is calculated on the fly depending on the
          regularisation parameter D. To change D, we need to redefine the
          function when we iterate over the grid.

    Notation: C_k, C_k+1 are the candidates _sets_ of the spectra S_k and S_k+1

    :param u: tuple, (layer, candidate), head-node, (i,j) in the paper

    :param v: tuple, (layer, candidate), tail-node, (r,s) in the paper

    :param **kwds : optional keyword parameters

    :return: tuple, (edge-weight, difference between order scores), edge-weight
        is used to determine the shortest path between first and last layer.
    """
    assert ("D" in kwargs.keys())
    assert ("use_sign" in kwargs.keys())
    assert ("epsilon_rt") in kwargs.keys()
    assert ("use_log") in kwargs.keys()

    # Additional keyword parameters
    D = kwargs["D"]
    # scalar, regularisation parameter controlling the influence of
    #     the weight of the edge (u,v), if D = 0, then only the score of the
    #     candidate are used, and no re-ranking is done.

    use_sign = kwargs["use_sign"]
    # boolean, should only the sign of the penalty be taken to calculate the
    #     shortest path?

    epsilon_rt = kwargs["epsilon_rt"]
    # scalar, order information of two consecutive layers is only taken into
    #     account of their retention time difference is larger than 'epsilon_rt'.

    use_log = kwargs["use_log"]
    # boolean, should the logarithm of the penalty be used instead?

    t,i = u # head-node: Notation in paper (i,j), Section 2.3.2
    tp1,j = v # tail-node: Notation in paper (r,s)

    # IOKR score of the tail-node
    s_tp1_j = cand_data[tp1]["iokrscores"][j]

    # When two spectra to have the same retention-time (but for examples different
    # m/z), than there is no order-information to exploit. We in that case give
    # the penalty zero, so that the best connection is the one connects the
    # highest IOKR scores.
    if np.abs (cand_data[t]["rt_cand_list"] - cand_data[tp1]["rt_cand_list"]) <= epsilon_rt:
        dW_tp1_j = 0
    else:
        dW_tp1_j = cand_data[t]["wtx"][i] - cand_data[tp1]["wtx"][j] # < 0 is good!

    if use_sign:
        dW_tp1_j = np.sign (dW_tp1_j)

    # Calculate penalty term as described in section 2.3.2
    penalty = max (0, dW_tp1_j)

    if use_log:
        # Log transform the penalty to avoid the outliers.
        penalty = np.log (penalty + 1)

    return (- s_tp1_j + D * penalty, dW_tp1_j)

def _load_candidate_fingerprints (spec, input_dir_candidates, predictor):
    """
    Load the candidate fingerprints of the specified msms-spectra.

    :param spec: string, identifier of the spectra and candidate list. Currently
        we use the inchikey of the structure represented by the spectra.

    :param input_dir_candidates: string, directory containing the scoring and
        fingerprints of the candidates (compare also 'build_candidate_structure').

    :param predictor: list of strings, containing the predictors used to train the model.
        Currently only 'maccs' and 'maccsCount_f2dcf0b3' are supported.

    :return: pandas.DataFrame, {"inchi": [...], "V1": [...], "V2": [...], ...}
        E.g.:
            "inchi","V1","V2",...
            "InChI=1S/C10H10N4O2S/c1-5-6(2)17-10(11-5)...",0,0,...
            ...
    """
    if predictor[0] == "maccs":
        fps_fn = "maccs_binary"
    elif predictor[0] == "maccsCount_f2dcf0b3":
        fps_fn = "maccs_count"
    else:
        raise ValueError ("Unsupported predictor for candidates: %s" % predictor[0])

    l_fps_files = os.listdir (input_dir_candidates + "/fingerprints/" + fps_fn + "/")
    cand_fps_fn = list (filter (re.compile ("fps_" + fps_fn + "_list.*=%s.csv" % spec).match, l_fps_files))
    assert (len (cand_fps_fn) == 1)
    cand_fps_fn = input_dir_candidates + "/fingerprints/" + fps_fn + "/" + cand_fps_fn[0]

    # Return the candidate fingerprints
    return DataFrame.from_csv (cand_fps_fn, index_col = None)

def _load_candidate_scorings (spec, input_dir_candidates, predictor):
    """
    Load the msms-based scores for the candidates of the specified msms-spectra.

    :param spec: string, identifier of the spectra and candidate list. Currently
        we use the inchikey of the structure represented by the spectra.

    :param input_dir_candidates: string, directory containing the scoring and
        fingerprints of the candidates (compare also 'build_candidate_structure').

    :param predictor: list of strings, containing the predictors used to train the model.
        Currently only 'maccs' and 'maccsCount_f2dcf0b3' are supported.

    :return: pandas.DataFrame, {"id1": [...], "score": [...]}
        E.g.:
            id1,score
            "InChI=1S/C10H10N4O2S/c11-8-1-3-9(4-2-8)17...",0.601026809509167
            "InChI=1S/C10H10N4O2S/c11-8-2-4-9(5-3-8)17...",0.59559886408
            ...

        NOTE: 'id1' here to the InChI, this can be changed, but we also need to modify
              '_process_single_candidate_list'.
    """
    if predictor[0] == "maccs":
        fps_fn = "maccs_binary"
    elif predictor[0] == "maccsCount_f2dcf0b3":
        fps_fn = "maccs_count"
    else:
        raise ValueError ("Unsupported predictor for candidates: %s" % predictor[0])

    l_scoring_files = os.listdir (input_dir_candidates + "/scorings/" + fps_fn + "/")
    scores_fn = list (filter (re.compile ("scoring_list.*=%s.csv" % spec).match, l_scoring_files))
    assert (len (scores_fn) == 1)
    scores_fn = input_dir_candidates + "/scorings/" + fps_fn + "/" + scores_fn[0]

    # Return scores in descending order
    return DataFrame.from_csv (scores_fn, index_col = None).sort_values ("score", ascending = False)


def perform_reranking_of_candidates (cand_data, topk = 25, cut_off_n_cand = np.inf,
                                     weight_function = None, **kwargs):
    """
    Function to re-rank sets of molecular candidates under exploitation of the
    msms-based scores and predicted retention orders.

    A special case: If topk = 1, than only the first shortest path is extracted.
                    This case is evaluated in the paper (Section 2.3).

    :param cand_data: list of dictionaries, containing the information about the
        candidate in each layer (see 'build_candidate_structure')

    :param cut_off_n_cand: intenger, maximum number of candidates considered in each
        layer. E.g. if set to 300 than only the 300 molecular candidates with the
        highest msms-based scores are used to find the shortest path.

    :param topk: integer, how many shortest path should be returned.

    :param weight_function: function, weight function to calculate the edge weights
        in the candidate graph (compare Figure 3).

    :param kwargs: dictionary, arguments passed to the weight function.

    :return: (topk_accs, paths, lengths)-tuple

        * topk accuracies, e.g. top1 accuracy = metabolite identification accuracy
        * path: topk shortest paths, list of list of integers (node ids)
        * lengts: list of lists of integers, length of the topk shortest paths
    """
    def _t_map (t, all_cand_blocked):
        return np.arange(0, len (all_cand_blocked))[~all_cand_blocked][t]

    cand_data_c = copy.deepcopy (cand_data)

    if weight_function is None:
        raise ValueError ("A weight function must be provided.")

    for t, _ in enumerate (cand_data_c):
        n_cand_t = len (cand_data_c[t]["iokrscores"])
        cand_data_c[t]["is_blocked"] = np.zeros (n_cand_t, dtype = "bool")

    num_correct = []
    paths = []
    lengths = []
    all_cand_blocked = np.zeros (len (cand_data_c), dtype = "bool")
    for k in range (topk):
        if all (all_cand_blocked):
            break

        # with Timer("Get shortest path for k = " + str (k + 1)):
        path, length = shortest_path_exclude_candidates (
            [cand_data_c[t] for t, _ in enumerate (cand_data_c) if not all_cand_blocked[t]],
            weight_function, exclude_blocked_candidates = True, cut_off_n_cand = cut_off_n_cand,
            check_input = True, **kwargs)

        assert (len (path) == np.sum (~all_cand_blocked))

        num_correct.append (0)
        paths.append (path)
        lengths.append (length)

        for t, cand in enumerate (path):
            num_correct[k] += cand_data_c[_t_map (t, all_cand_blocked)]["is_true_identification"][cand]

            # Block candidates along the current shortest path
            cand_data_c[_t_map (t, all_cand_blocked)]["is_blocked"][cand] = True

        for t, _ in enumerate (cand_data_c):
            all_cand_blocked[t] = all (cand_data_c[t]["is_blocked"])

    # Get the top-k accuracies for different k
    topk_accs = np.cumsum (num_correct) / len (cand_data_c) * 100 # k \in [1,topk]

    return topk_accs, paths, lengths


def _candidate_iokrscores_and_rankingscores (
        spec, input_dir_candidates, model, msms_rts, d_inchikey2inchi, layer):
    """
    Construct the dictionary containing the msms-based and retention order scores
    for each molecular candidated in the current layer.

    :param spec: string, unique identifier for the spectrum in the layer to process.
        Currently we use the inchikey of the corresponding molecular structure for that.

    :param input_dir_candidates: string, directory containing the scoring and
        fingerprints of the candidates (compare also 'build_candidate_structure').

    :param model: dictionary, containing the order prediction model

        "ranking_model": Currently only _fitted_ (see .fit(...)) KernelRankSVC objects are supported
        "predictor": list of strings, containing the predictors used to train the model.
                     Currently only 'maccs' and 'maccsCount_f2dcf0b3' are supported.

    :param msms_rts: pandas.DataFrame, containing the content of the 'msms_rts.csv'
        file. (compare also 'build_candidate_structure')

    :param d_inchikey2inchi: dictionary, keys: inchikeys, values: corresponding inchis,
        for all the MSMS spectra

    :param layer: integer, layer index

    :return: dictonary, containing the information about the candidates of the current layer.
    """
    def _process_single_candidate_list (
            spec, scores, cand_fps, msms_rts, d_inchikey2inchi, layer, model):
        """
        :param spec: string, identifier of the spectra and candidate list. Currently
            we use the inchikey of the structure represented by the spectra.

        :param scores: pandas.DataFrame, shape = (n_cand, 2), two-column data-frame:

            "id1",   "score"
             INCHI-1, score_1
             INCHI-2, score_2
             ...

             The table must be sorted according to the score of the candidates descending.

        :param msms_rts: pandas.DataFrame, shape = (n_spec, 3), containing the information
            about the inchikey, inchi and retention time of each msms-spectra.

        :param d_inchikey2inchi: dictionary, mapping from the inchikey (spectra and
            candidate list identifier) to the inchi of the correct / true candidate.

            :key: string, inchikey
            :value: string, inchi

        :param layer: integer, layer index

        :param model: dictionary, containing the order prediction model

            "ranking_model": Currently only _fitted_ (see .fit(...)) KernelRankSVC objects are supported
            "predictor": list of strings, containing the predictors used to train the model.
                         Currently only 'maccs' and 'maccsCount_f2dcf0b3' are supported.

        :param add_ranking_score: boolean, should the ranking / preference score be
            added to each node, e.g. RankSVM: w^T\phi_i

        :return: dictonary, containing the information about the candidates
            of the current layer.
        """
        rt_cand_list = msms_rts[msms_rts.inchikey == spec].rt.values
        assert (len (rt_cand_list) == 1)
        rt_cand_list = rt_cand_list[0]
        # Determine the index, i.e. row in the scores-DataFrame, if the correct / true
        # candidate.
        inchi_correct_cand = d_inchikey2inchi[spec]

        # FIXME: 'id1' corresponds the InChIs of the candidates. We should fix the scoring files.
        assert (sum (scores.id1 == inchi_correct_cand) == 1)

        fps_correct_cand = cand_fps[cand_fps.inchi == inchi_correct_cand].drop ("inchi", axis = 1).values
        K_candcand = pairwise_kernels (
            fps_correct_cand, fps_correct_cand,
            metric = model["ranking_model"].kernel, filter_params = True)

        # Output lists
        # Note: The candidate are sorted according to their IOKR score (descending)
        #       see '_load_candidate_scorings'.
        iokrscores = []
        ranks = []
        is_true_identification = []
        d_squared = []
        wtx = []
        id1s = []

        rank = 0 # smallest rank is one which is assigned to the largest score.
        rank_correct_cand = 0
        last_score = np.inf
        for id1, score in scores[["id1", "score"]].values:
            if last_score > score:
                last_score = score
                rank += 1

            # Calculat the similarity (in terms of the used fingerprints for the RankSVM)
            # between the candidate and the correct / true identification.
            fps_cand_id1 = cand_fps[cand_fps.inchi == id1].drop ("inchi", axis = 1).values
            K_ii = pairwise_kernels (
                fps_cand_id1, fps_cand_id1,
                metric = model["ranking_model"].kernel, filter_params = True)
            K_icand = pairwise_kernels (
                fps_cand_id1, fps_correct_cand,
                metric = model["ranking_model"].kernel, filter_params = True)

            iokrscores.append (score)
            ranks.append (rank)
            is_true_identification.append (inchi_correct_cand == id1)
            d_squared.append ((K_ii + K_candcand - 2 * K_icand)[0])
            wtx.append (model["ranking_model"].map_values (fps_cand_id1)[0])
            id1s.append (id1)

            if inchi_correct_cand == id1:
                rank_correct_cand = rank

        assert (rank_correct_cand > 0)

        return {"iokrscores": iokrscores,                           # msms-based scores of the candidates

                "ranks": ranks,                                     # dense rank for each candidate based on
                                                                    # the msms scores

                "is_true_identification": is_true_identification,   # boolean vector indicating the whether
                                                                    # a candidate is the true identification,
                                                                    # i.e. the correct candidate

                "d_squared": d_squared,                             # euclidean distance of the each candidates'
                                                                    # feature vector (fp) to the one of the
                                                                    # correct candidate (used the models kernel)

                "wtx": wtx,                                         # retention order score predicted using the
                                                                    # KernelRankSVC implementation

                "inchis": id1s,                                     # candidate inchies

                "rt_cand_list": rt_cand_list,                       # retention time associated with the
                                                                    # candidate list

                "rank_correct_cand": rank_correct_cand,             # rank of the correct candidates, based
                                                                    # on the msms-score.

                "inchi_correct_cand": inchi_correct_cand,           # inchi of the correct candidate

                "spec_id": spec}                                    # spectra identifier

    # Load candidate scores and fps correponding to the model predictor
    scores = _load_candidate_scorings (spec, input_dir_candidates, model["predictor"])
    cand_fps = _load_candidate_fingerprints (spec, input_dir_candidates, model["predictor"])

    # Add the nodes for all candidates
    return _process_single_candidate_list (
        spec, scores, cand_fps, msms_rts, d_inchikey2inchi, layer, model)
