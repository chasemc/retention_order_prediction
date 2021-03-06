# Overview

Scripts used to run the experiments presented in the paper:

__"Liquid-Chromatography Retention Order Prediction for Metabolite Identification"__,

_Eric Bach, Sandor Szedmak, Celine Brouard, Sebastian Böcker and Juho Rousu_, 2018

[Summary of the results](results/ECCB2018.html) shown in the paper (File needs
to be downloaded and opened with a web-browser.). 

## TODO:

### Documentation of the repository

- Add description how to use the MACCS counting FPS using the modified CDK.

# Installation

There is no further installation required. The scripts run out of the box, if 
all the package dependencies are sattisfied. All the __source code__ in this repository
is under [the MIT License](LICENSE.txt).

## Order prediction and evaluation code

The [order predictor, e.g. RankSVM, and evaluation scripts are implemented in Python](src/). 
The code has been tested with Python 3.5 and 3.6. The following packages are required:

- scipy >= 0.19.1
- json >= 2.0.9
- numpy >= 1.13.1
- joblib >= 0.11
- pandas >= 0.20.3
- sklearn >= 0.19.0
- networkx >= 2.0
- matplotlib >= 2.1 (optional)

## Data pre-processing and evaluation report creation

The data [pre-processing scripts](data/scripts) as well as the [script to reproduce the results](results/scripts)
shown in the paper are written in R. For the development R version 3.4 was used. 
The following packages are required:

- Reproduction of results: [ECCB2018.Rmd](results/scripts/ECCB2018.Rmd)
    - data.table 
    - ggplot2 
    - knitr 
- Reproduction of data pre-processing:
    - Matrix
    - [obabel2R](https://gitlab.com/R_packages/obabel2R)
    - [rcdk](https://github.com/rajarshi/cdkr) (used for [fingerprint calculation](data/processed/README.md#fingerprint-calculation))
    - fingerprint

Furthermore, the [OpenBabel](http://openbabel.org/wiki/Main_Page) (>= 2.3.2) 
command line tool ```obabel``` must be installed __only if__ the data 
pre-processing needs to be repeated.

# Usage

All experiments of the paper can be reproduced by using the calling the [evaluation_scenarios_main.py](src/evaluation_scenarios_main.py)
script with the proper parameters:

```
usage: evaluation_scenarios_main.py <ESTIMATOR> <SCENARIO> <SYSSET> <TSYSIDX> <PATH/TO/CONFIG.JSON> <NJOBS> <DEBUG>
  ESTIMATOR:           {'ranksvm', 'svr'}, which order predictor to use.
  SCENARIO:            {'baseline', 'baseline_single', 'baseline_single_perc', 'all_on_one', 'all_on_one_perc', 'met_ident_perf_GS_BS'}, which experiment to run.
  SYSSET:              {10, imp, 10_imp}, which set of systems to train on.
  TSYSIDX:             {-1, 0, ..., |sysset| - 1}, which target system to use for evaluation.
  PATH/TO/CONFIG.JSON: configuration file, e.g. PredRet/v2/config.json
  NJOBS:               How many jobs should run in parallel for hyper-parameter estimation?
  DEBUG:               {True, False}, should we run a smoke test.
```

| __SCENARIO__ | __Description__ | __Reference in the Paper__ |
| ------------ | --------------- | -------------------------- |
| [```baseline_single```](src/evaluation_scenarios_main.py#L708) | Single system used as training and target | Table 3, Table 4 (first two columns) |
| [```baseline_single_perc```](src/evaluation_scenarios_main.py#L737) | Single system used as training and target. Different percentage of data used for trainging. | Figure 4 (stroked lines) |
| [```all_on_one```](src/evaluation_scenarios_main.py#L615) | All systems used for training. Single system used as target. Target system in training (LTSO): True & False | Table 4, LTSO = True 3. & 4. column, LTSO = False 5. & 6. column |
| [```all_on_one_perc```](src/evaluation_scenarios_main.py#L662) | All systems used for training. Single system used as target. Varying percentage of target system data used for training | Figure 4 (solid lines) |

## Example: Reproducing results shown in Table 3:

The following function calls are need:

__MACCS counting fingerprints:__

```
python src/evaluation_scenarios_main.py ranksvm baseline_single 10 -1 results/raw/PredRet/v2/config.json 2 False
```

- [```baseline_single```](src/evaluation_scenarios_main.py#L708): Single system used for training and testing.
- [```10```](results/raw/PredRet/v2/config.json#L7): Use "Eawag_XBridgeC18", "FEM_long", "RIKEN", "UFZ_Phenomenex", "LIFE_old" for training and testing.
- ```-1```: By setting TSYSIDX to -1, we run all target systems in a single job. [This parameter can be used for parallelization](results/scripts/makefiles#combining-evaluation-results-from-parallel-runs).
- [```results/raw/PredRet/v2/config.json```](results/raw/PredRet/v2/config.json): Configuration of the experiment, e.g. [molecular features and kernels](results/raw/PredRet/v2/config.json#L28).
- ```2```: Number of jobs/cpus used for the [hyper-parameter search](src/model_selection_cls.py#L370).
- ```False```: Not running in debug-mode. Results will be stored in the [final](results/raw/PredRet/v2/final) directory.

The results will be stored into: 

```
results/PredRet/v2
                └── final
                    └── ranksvm_slacktype=on_pairs
                        └── allow_overlap=True_d_lower=0_d_upper=16_ireverse=False_type=order_graph
                            └── difference
                                └── maccsCount_f2dcf0b3
                                    └── minmax
                                        └── baseline_single
```

__MACCS binary fingerprints:__

Modify the [```results/raw/PredRet/v2/config.json```](results/raw/PredRet/v2/config.json)
configuration file:

```json
"molecule_representation": {
  "kernel": "minmax",
  "predictor": ["maccsCount_f2dcf0b3"],
  "feature_scaler": "noscaling",
  "poly_feature_exp": false
}
```

becomes

```json
"molecule_representation": {
  "kernel": "tanimoto",
  "predictor": ["maccs"],
  "feature_scaler": "noscaling",
  "poly_feature_exp": false
}
```

Then run:

```
python src/evaluation_scenarios_main.py ranksvm baseline_single 10 -1 results/raw/PredRet/v2/config.json 2 False
```

The results will be stored into:

```
results/PredRet/v2
                └── final
                    └── ranksvm_slacktype=on_pairs
                        └── allow_overlap=True_d_lower=0_d_upper=16_ireverse=False_type=order_graph
                            └── difference
                                └── maccs
                                    └── tanimoto
                                        └── baseline_single
```

How the results can be loaded and visualized is described [here](results/scripts/README.md#helperr-load-results-in-to-r).

# Citation

To refer the original publication please use:

```bibtex
@article{doi:10.1093/bioinformatics/bty590,
    author  = {Bach, Eric and Szedmak, Sandor and Brouard, Céline and Böcker, Sebastian and Rousu, Juho},
    title   = {Liquid-chromatography retention order prediction for metabolite identification},
    journal = {Bioinformatics},
    volume  = {34},
    number  = {17},
    pages   = {i875-i883},
    year    = {2018},
    doi     = {10.1093/bioinformatics/bty590},
    URL     = {http://dx.doi.org/10.1093/bioinformatics/bty590},
    eprint  = {/oup/backfile/content_public/journal/bioinformatics/34/17/10.1093_bioinformatics_bty590/2/bty590.pdf}
}
```