# LoCoMo results

EXPLORATORY. Plan, definitions and the rules fixed before any run: `docs/LOCOMO-PLAN.md`. Regenerate with `python -m evalkit.locomo_report <run-dir>`.

## Completion

| Planned | Completed |
|---|---|
| A0 on all eligible questions: 1536 | 1536 |
| B1 (A1 and A1-tree-only on the seeded stratified subset): 100 | 100 |
| SHUFFLED-SUMMARIES on the first 30 subset questions | 30 |
| B2 (optional secondary batch) | 300 |

Run state: COMPLETE. Model calls charged: 3246 of cap 9000. Models reported by the CLI: ['claude-sonnet-5-5']. Code commit: 3c8caa2998f97480e5a30e9c97b44e4b176177b4.
Pool sizes (leaves per conversation, file order): 34, 25, 47, 42, 52, 60, 49, 50, 36, 43. These pools are small: see the chance reference in every table.

## A0 on all eligible questions (no model call)

n = 1536 questions.

| Metric | A0 [95% CI, primary cluster] | conversation-cluster CI |
|---|---|---|
| gold-leaf recall@5 | 0.784 [0.760, 0.806] | 0.784 [0.753, 0.816] |
| gold-leaf recall@10 | 0.892 [0.876, 0.907] | 0.892 [0.868, 0.913] |
| gold-session recall@5 | 0.813 [0.791, 0.835] | 0.813 [0.784, 0.844] |
| gold-session recall@10 | 0.921 [0.908, 0.935] | 0.921 [0.899, 0.942] |
| MRR@10 (leaf) | 0.674 [0.652, 0.696] | 0.674 [0.656, 0.694] |
| all-gold@10 (leaf, >=2 gold leaves) | 0.497 [0.443, 0.552] | 0.497 [0.425, 0.570] |
| all-gold@10 (session, >=2 gold sessions) | 0.539 [0.485, 0.592] | 0.539 [0.477, 0.612] |
| chance reference, gold-leaf recall@5 / @10 | 0.117 / 0.234 | |

### A0 by category

| Category | n | gold-leaf recall@10 | gold-session recall@5 |
|---|---|---|---|
| 1 | 282 | 0.754 [0.716, 0.790] | 0.599 [0.558, 0.639] |
| 2 | 321 | 0.922 [0.889, 0.951] | 0.832 [0.789, 0.874] |
| 3 | 92 | 0.669 [0.584, 0.751] | 0.556 [0.458, 0.652] |
| 4 | 841 | 0.951 [0.934, 0.967] | 0.906 [0.882, 0.928] |

### A0 by conversation

| Conversation | leaves | n | leaf@5 | leaf@10 | session@5 |
|---|---|---|---|---|---|
| 0 | 34 | 150 | 0.790 | 0.885 | 0.832 |
| 1 | 25 | 81 | 0.824 | 0.948 | 0.849 |
| 2 | 47 | 152 | 0.775 | 0.862 | 0.785 |
| 3 | 42 | 199 | 0.756 | 0.909 | 0.785 |
| 4 | 52 | 178 | 0.809 | 0.905 | 0.848 |
| 5 | 60 | 123 | 0.696 | 0.807 | 0.743 |
| 6 | 49 | 150 | 0.886 | 0.924 | 0.894 |
| 7 | 50 | 191 | 0.829 | 0.936 | 0.865 |
| 8 | 36 | 156 | 0.728 | 0.882 | 0.747 |
| 9 | 43 | 156 | 0.741 | 0.850 | 0.782 |

## Tree arms, batch B1

### A0, A1 and A1-tree-only on the same 100 questions

n = 100 questions. Cells are mean [95% CI], percentile clustered bootstrap, primary cluster (conversation x session of first evidence), 10,000 resamples, seed 16096.

| Metric (n with the metric) | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 (n=100) | 0.828 [0.762, 0.889] | 0.889 [0.832, 0.939] | 0.901 [0.845, 0.950] |
| gold-leaf recall@10 (n=100) | 0.907 [0.855, 0.952] | 0.948 [0.903, 0.983] | 0.953 [0.910, 0.987] |
| gold-session recall@5 (n=100) | 0.875 [0.815, 0.929] | 0.920 [0.869, 0.963] | 0.918 [0.865, 0.963] |
| gold-session recall@10 (n=100) | 0.940 [0.899, 0.975] | 0.961 [0.921, 0.991] | 0.953 [0.910, 0.987] |
| MRR@10 (leaf) (n=100) | 0.675 [0.605, 0.745] | 0.800 [0.735, 0.860] | 0.690 [0.621, 0.758] |
| all-gold@10 (leaf, >=2 gold leaves) (n=18) | 0.500 [0.278, 0.722] | 0.778 [0.556, 0.947] | 0.889 [0.722, 1.000] |
| all-gold@10 (session, >=2 gold sessions) (n=18) | 0.556 [0.294, 0.789] | 0.833 [0.647, 1.000] | 0.889 [0.722, 1.000] |
| chance reference, gold-leaf recall@5 / @10 | 0.117 / 0.234 | 0.117 / 0.234 | 0.117 / 0.234 |

Conversation-only clusters (10 clusters at most), the sensitivity interval:

| Metric | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 | 0.828 [0.778, 0.876] | 0.889 [0.841, 0.936] | 0.901 [0.859, 0.942] |
| gold-leaf recall@10 | 0.907 [0.882, 0.929] | 0.948 [0.919, 0.978] | 0.953 [0.929, 0.980] |
| gold-session recall@5 | 0.875 [0.822, 0.929] | 0.920 [0.883, 0.954] | 0.918 [0.884, 0.951] |
| gold-session recall@10 | 0.940 [0.913, 0.968] | 0.961 [0.934, 0.985] | 0.953 [0.929, 0.980] |
| MRR@10 (leaf) | 0.675 [0.620, 0.728] | 0.800 [0.762, 0.837] | 0.690 [0.637, 0.732] |
| all-gold@10 (leaf, >=2 gold leaves) | 0.500 [0.333, 0.750] | 0.778 [0.611, 0.933] | 0.889 [0.688, 1.000] |
| all-gold@10 (session, >=2 gold sessions) | 0.556 [0.400, 0.769] | 0.833 [0.636, 0.963] | 0.889 [0.688, 1.000] |

Paired differences (mean of per-question differences, same clustering):

| Metric | A1 minus A0 | A1-tree-only minus A0 | A1 minus A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 | 0.060 [-0.002, 0.125] (conv: [0.005, 0.108]) | 0.073 [-0.003, 0.149] (conv: [0.009, 0.129]) | -0.013 [-0.056, 0.032] (conv: [-0.048, 0.024]) |
| gold-leaf recall@10 | 0.041 [-0.017, 0.099] (conv: [0.000, 0.086]) | 0.046 [-0.012, 0.106] (conv: [0.009, 0.088]) | -0.006 [-0.015, 0.000] (conv: [-0.013, 0.000]) |
| gold-session recall@5 | 0.045 [-0.013, 0.104] (conv: [-0.008, 0.088]) | 0.043 [-0.021, 0.108] (conv: [-0.013, 0.094]) | 0.003 [-0.019, 0.029] (conv: [-0.016, 0.021]) |
| gold-session recall@10 | 0.021 [-0.023, 0.064] (conv: [-0.021, 0.061]) | 0.013 [-0.036, 0.060] (conv: [-0.028, 0.057]) | 0.007 [-0.007, 0.030] (conv: [0.000, 0.021]) |
| MRR@10 (leaf) | 0.125 [0.069, 0.182] (conv: [0.084, 0.161]) | 0.014 [-0.068, 0.101] (conv: [-0.053, 0.071]) | 0.110 [0.050, 0.171] (conv: [0.071, 0.158]) |
| all-gold@10 (leaf, >=2 gold leaves) | 0.278 [0.000, 0.526] (conv: [0.000, 0.483]) | 0.389 [0.118, 0.647] (conv: [0.000, 0.630]) | -0.111 [-0.294, 0.000] (conv: [-0.211, 0.000]) |
| all-gold@10 (session, >=2 gold sessions) | 0.278 [0.000, 0.529] (conv: [0.000, 0.483]) | 0.333 [0.056, 0.611] (conv: [0.000, 0.552]) | -0.056 [-0.176, 0.000] (conv: [-0.167, 0.000]) |

### Per category

Categories are labelled by number (the LoCoMo repository does not name them). Category 3 has few questions (about 6 in B1): its intervals are printed but are not interpretable at that size.

| Category | n | A0 (flat) leaf@10 | A1 (tree + flat, RRF) leaf@10 | A1-tree-only leaf@10 | A0 (flat) sess@5 | A1 (tree + flat, RRF) sess@5 | A1-tree-only sess@5 | A1 minus A0 leaf@10 |
|---|---|---|---|---|---|---|---|---|
| 1 | 18 | 0.816 [0.724, 0.903] | 0.931 [0.863, 0.984] | 0.963 [0.907, 1.000] | 0.695 [0.535, 0.839] | 0.836 [0.724, 0.932] | 0.878 [0.780, 0.956] | 0.114 [0.020, 0.209] |
| 2 | 21 | 0.952 [0.850, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.857 [0.700, 1.000] | 1.000 [1.000, 1.000] | 0.952 [0.870, 1.000] | 0.048 [0.000, 0.150] |
| 3 | 6 | 0.833 [0.500, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.833 [0.500, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.167 [0.000, 0.500] |
| 4 | 55 | 0.927 [0.852, 0.982] | 0.927 [0.849, 0.983] | 0.927 [0.849, 0.983] | 0.945 [0.877, 1.000] | 0.909 [0.825, 0.981] | 0.909 [0.825, 0.981] | 0.000 [-0.091, 0.088] |

Tree recalls that failed (scored 0, kept): 0 of 100. A0 at recall time equals the A0 of the all-questions stage on 100 of 100 questions.
Routing: in 100 of 100 recalls the number of holder calls equals the number of level-1 nodes of the conversation's tree (3, 2, 3, 3, 4, 4, 4, 4, 3, 3), that is, the recall opened every node and the model was offered every leaf of the pool. Where this is all of them, A1 and A1-tree-only measure a model pass over the leaf descriptors of the whole pool compared with flat search, not a selective descent of the tree.

## Tree arms, batch B2

### A0, A1 and A1-tree-only on the same 300 questions

n = 300 questions. Cells are mean [95% CI], percentile clustered bootstrap, primary cluster (conversation x session of first evidence), 10,000 resamples, seed 16096.

| Metric (n with the metric) | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 (n=300) | 0.754 [0.706, 0.799] | 0.862 [0.830, 0.894] | 0.854 [0.817, 0.889] |
| gold-leaf recall@10 (n=300) | 0.860 [0.822, 0.894] | 0.935 [0.911, 0.958] | 0.907 [0.878, 0.935] |
| gold-session recall@5 (n=300) | 0.790 [0.743, 0.833] | 0.894 [0.864, 0.921] | 0.881 [0.846, 0.913] |
| gold-session recall@10 (n=300) | 0.904 [0.873, 0.932] | 0.950 [0.928, 0.970] | 0.937 [0.912, 0.960] |
| MRR@10 (leaf) (n=300) | 0.638 [0.592, 0.685] | 0.761 [0.726, 0.795] | 0.680 [0.641, 0.718] |
| all-gold@10 (leaf, >=2 gold leaves) (n=71) | 0.507 [0.386, 0.625] | 0.817 [0.721, 0.899] | 0.803 [0.706, 0.889] |
| all-gold@10 (session, >=2 gold sessions) (n=69) | 0.565 [0.439, 0.681] | 0.826 [0.731, 0.909] | 0.812 [0.714, 0.897] |
| chance reference, gold-leaf recall@5 / @10 | 0.116 / 0.232 | 0.116 / 0.232 | 0.116 / 0.232 |

Conversation-only clusters (10 clusters at most), the sensitivity interval:

| Metric | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 | 0.754 [0.721, 0.792] | 0.862 [0.830, 0.892] | 0.854 [0.802, 0.896] |
| gold-leaf recall@10 | 0.860 [0.814, 0.898] | 0.935 [0.916, 0.954] | 0.907 [0.874, 0.936] |
| gold-session recall@5 | 0.790 [0.752, 0.829] | 0.894 [0.866, 0.919] | 0.881 [0.841, 0.915] |
| gold-session recall@10 | 0.904 [0.861, 0.940] | 0.950 [0.938, 0.962] | 0.937 [0.910, 0.962] |
| MRR@10 (leaf) | 0.638 [0.581, 0.691] | 0.761 [0.732, 0.788] | 0.680 [0.649, 0.705] |
| all-gold@10 (leaf, >=2 gold leaves) | 0.507 [0.359, 0.630] | 0.817 [0.734, 0.885] | 0.803 [0.686, 0.904] |
| all-gold@10 (session, >=2 gold sessions) | 0.565 [0.417, 0.688] | 0.826 [0.731, 0.898] | 0.812 [0.683, 0.916] |

Paired differences (mean of per-question differences, same clustering):

| Metric | A1 minus A0 | A1-tree-only minus A0 | A1 minus A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 | 0.109 [0.072, 0.147] (conv: [0.079, 0.139]) | 0.100 [0.050, 0.150] (conv: [0.062, 0.136]) | 0.009 [-0.026, 0.043] (conv: [-0.027, 0.042]) |
| gold-leaf recall@10 | 0.076 [0.042, 0.111] (conv: [0.049, 0.112]) | 0.048 [0.008, 0.087] (conv: [0.033, 0.065]) | 0.028 [0.007, 0.052] (conv: [0.012, 0.050]) |
| gold-session recall@5 | 0.104 [0.068, 0.142] (conv: [0.081, 0.126]) | 0.091 [0.042, 0.142] (conv: [0.056, 0.123]) | 0.013 [-0.018, 0.044] (conv: [-0.019, 0.042]) |
| gold-session recall@10 | 0.046 [0.016, 0.077] (conv: [0.018, 0.081]) | 0.033 [-0.000, 0.065] (conv: [0.012, 0.056]) | 0.013 [-0.005, 0.032] (conv: [-0.004, 0.031]) |
| MRR@10 (leaf) | 0.123 [0.087, 0.158] (conv: [0.093, 0.155]) | 0.042 [-0.015, 0.096] (conv: [-0.013, 0.100]) | 0.081 [0.043, 0.120] (conv: [0.045, 0.116]) |
| all-gold@10 (leaf, >=2 gold leaves) | 0.310 [0.191, 0.429] (conv: [0.204, 0.427]) | 0.296 [0.184, 0.409] (conv: [0.181, 0.412]) | 0.014 [-0.030, 0.062] (conv: [-0.031, 0.066]) |
| all-gold@10 (session, >=2 gold sessions) | 0.261 [0.145, 0.380] (conv: [0.161, 0.366]) | 0.246 [0.125, 0.375] (conv: [0.135, 0.338]) | 0.014 [-0.032, 0.065] (conv: [-0.032, 0.067]) |

### Per category

Categories are labelled by number (the LoCoMo repository does not name them). Category 3 has few questions (about 6 in B1): its intervals are printed but are not interpretable at that size.

| Category | n | A0 (flat) leaf@10 | A1 (tree + flat, RRF) leaf@10 | A1-tree-only leaf@10 | A0 (flat) sess@5 | A1 (tree + flat, RRF) sess@5 | A1-tree-only sess@5 | A1 minus A0 leaf@10 |
|---|---|---|---|---|---|---|---|---|
| 1 | 55 | 0.708 [0.622, 0.788] | 0.911 [0.863, 0.955] | 0.892 [0.835, 0.945] | 0.588 [0.493, 0.680] | 0.712 [0.635, 0.785] | 0.802 [0.713, 0.883] | 0.203 [0.128, 0.285] |
| 2 | 63 | 0.921 [0.848, 0.984] | 0.968 [0.918, 1.000] | 0.984 [0.950, 1.000] | 0.802 [0.700, 0.891] | 0.960 [0.908, 1.000] | 0.944 [0.885, 0.992] | 0.048 [0.000, 0.106] |
| 3 | 18 | 0.662 [0.456, 0.843] | 0.750 [0.529, 0.941] | 0.676 [0.451, 0.870] | 0.505 [0.309, 0.689] | 0.634 [0.447, 0.806] | 0.644 [0.454, 0.821] | 0.088 [0.017, 0.176] |
| 4 | 164 | 0.909 [0.861, 0.952] | 0.951 [0.919, 0.981] | 0.909 [0.865, 0.948] | 0.884 [0.829, 0.933] | 0.957 [0.926, 0.983] | 0.909 [0.863, 0.948] | 0.043 [-0.006, 0.092] |

Tree recalls that failed (scored 0, kept): 0 of 300. A0 at recall time equals the A0 of the all-questions stage on 300 of 300 questions.
Routing: in 300 of 300 recalls the number of holder calls equals the number of level-1 nodes of the conversation's tree (3, 2, 3, 3, 4, 4, 4, 4, 3, 3), that is, the recall opened every node and the model was offered every leaf of the pool. Where this is all of them, A1 and A1-tree-only measure a model pass over the leaf descriptors of the whole pool compared with flat search, not a selective descent of the tree.

## Tree arms, batch B1+B2

### A0, A1 and A1-tree-only on the same 400 questions

n = 400 questions. Cells are mean [95% CI], percentile clustered bootstrap, primary cluster (conversation x session of first evidence), 10,000 resamples, seed 16096.

| Metric (n with the metric) | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 (n=400) | 0.772 [0.734, 0.810] | 0.869 [0.840, 0.897] | 0.866 [0.835, 0.895] |
| gold-leaf recall@10 (n=400) | 0.871 [0.840, 0.901] | 0.938 [0.918, 0.958] | 0.919 [0.895, 0.942] |
| gold-session recall@5 (n=400) | 0.811 [0.774, 0.847] | 0.900 [0.876, 0.924] | 0.890 [0.861, 0.917] |
| gold-session recall@10 (n=400) | 0.913 [0.887, 0.937] | 0.953 [0.934, 0.970] | 0.941 [0.920, 0.961] |
| MRR@10 (leaf) (n=400) | 0.647 [0.609, 0.685] | 0.771 [0.741, 0.801] | 0.682 [0.649, 0.716] |
| all-gold@10 (leaf, >=2 gold leaves) (n=89) | 0.506 [0.393, 0.616] | 0.809 [0.723, 0.889] | 0.820 [0.739, 0.894] |
| all-gold@10 (session, >=2 gold sessions) (n=87) | 0.563 [0.450, 0.675] | 0.828 [0.744, 0.905] | 0.828 [0.750, 0.900] |
| chance reference, gold-leaf recall@5 / @10 | 0.116 / 0.233 | 0.116 / 0.233 | 0.116 / 0.233 |

Conversation-only clusters (10 clusters at most), the sensitivity interval:

| Metric | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 | 0.772 [0.738, 0.810] | 0.869 [0.839, 0.896] | 0.866 [0.822, 0.902] |
| gold-leaf recall@10 | 0.871 [0.833, 0.903] | 0.938 [0.921, 0.955] | 0.919 [0.891, 0.942] |
| gold-session recall@5 | 0.811 [0.773, 0.848] | 0.900 [0.876, 0.922] | 0.890 [0.856, 0.918] |
| gold-session recall@10 | 0.913 [0.878, 0.943] | 0.953 [0.940, 0.964] | 0.941 [0.918, 0.964] |
| MRR@10 (leaf) | 0.647 [0.602, 0.692] | 0.771 [0.747, 0.793] | 0.682 [0.659, 0.700] |
| all-gold@10 (leaf, >=2 gold leaves) | 0.506 [0.370, 0.644] | 0.809 [0.729, 0.873] | 0.820 [0.716, 0.907] |
| all-gold@10 (session, >=2 gold sessions) | 0.563 [0.446, 0.685] | 0.828 [0.741, 0.888] | 0.828 [0.717, 0.917] |

Paired differences (mean of per-question differences, same clustering):

| Metric | A1 minus A0 | A1-tree-only minus A0 | A1 minus A1-tree-only |
|---|---|---|---|
| gold-leaf recall@5 | 0.097 [0.064, 0.129] (conv: [0.068, 0.126]) | 0.093 [0.052, 0.135] (conv: [0.055, 0.128]) | 0.003 [-0.024, 0.031] (conv: [-0.020, 0.028]) |
| gold-leaf recall@10 | 0.067 [0.037, 0.098] (conv: [0.043, 0.094]) | 0.047 [0.015, 0.081] (conv: [0.031, 0.064]) | 0.019 [0.003, 0.037] (conv: [0.008, 0.035]) |
| gold-session recall@5 | 0.089 [0.057, 0.122] (conv: [0.065, 0.112]) | 0.079 [0.038, 0.119] (conv: [0.046, 0.110]) | 0.010 [-0.013, 0.035] (conv: [-0.010, 0.031]) |
| gold-session recall@10 | 0.040 [0.014, 0.066] (conv: [0.016, 0.066]) | 0.028 [-0.000, 0.056] (conv: [0.008, 0.049]) | 0.012 [-0.002, 0.026] (conv: [-0.002, 0.025]) |
| MRR@10 (leaf) | 0.123 [0.092, 0.155] (conv: [0.095, 0.152]) | 0.035 [-0.011, 0.081] (conv: [-0.006, 0.081]) | 0.088 [0.057, 0.121] (conv: [0.064, 0.112]) |
| all-gold@10 (leaf, >=2 gold leaves) | 0.303 [0.196, 0.411] (conv: [0.181, 0.420]) | 0.315 [0.207, 0.422] (conv: [0.169, 0.443]) | -0.011 [-0.061, 0.037] (conv: [-0.044, 0.028]) |
| all-gold@10 (session, >=2 gold sessions) | 0.264 [0.159, 0.370] (conv: [0.153, 0.368]) | 0.264 [0.151, 0.379] (conv: [0.139, 0.364]) | 0.000 [-0.045, 0.047] (conv: [-0.041, 0.047]) |

### Per category

Categories are labelled by number (the LoCoMo repository does not name them). Category 3 has few questions (about 6 in B1): its intervals are printed but are not interpretable at that size.

| Category | n | A0 (flat) leaf@10 | A1 (tree + flat, RRF) leaf@10 | A1-tree-only leaf@10 | A0 (flat) sess@5 | A1 (tree + flat, RRF) sess@5 | A1-tree-only sess@5 | A1 minus A0 leaf@10 |
|---|---|---|---|---|---|---|---|---|
| 1 | 73 | 0.735 [0.664, 0.807] | 0.916 [0.877, 0.952] | 0.909 [0.864, 0.950] | 0.614 [0.530, 0.698] | 0.742 [0.677, 0.805] | 0.821 [0.752, 0.885] | 0.181 [0.115, 0.247] |
| 2 | 84 | 0.929 [0.869, 0.977] | 0.976 [0.938, 1.000] | 0.988 [0.963, 1.000] | 0.815 [0.735, 0.892] | 0.970 [0.931, 1.000] | 0.946 [0.895, 0.988] | 0.048 [0.011, 0.098] |
| 3 | 24 | 0.705 [0.527, 0.860] | 0.812 [0.636, 0.958] | 0.757 [0.576, 0.910] | 0.587 [0.402, 0.760] | 0.726 [0.562, 0.867] | 0.733 [0.572, 0.875] | 0.108 [0.024, 0.217] |
| 4 | 219 | 0.913 [0.871, 0.952] | 0.945 [0.916, 0.972] | 0.913 [0.878, 0.947] | 0.900 [0.858, 0.938] | 0.945 [0.915, 0.972] | 0.909 [0.870, 0.945] | 0.032 [-0.010, 0.078] |

Tree recalls that failed (scored 0, kept): 0 of 400. A0 at recall time equals the A0 of the all-questions stage on 400 of 400 questions.
Routing: in 400 of 400 recalls the number of holder calls equals the number of level-1 nodes of the conversation's tree (3, 2, 3, 3, 4, 4, 4, 4, 3, 3), that is, the recall opened every node and the model was offered every leaf of the pool. Where this is all of them, A1 and A1-tree-only measure a model pass over the leaf descriptors of the whole pool compared with flat search, not a selective descent of the tree.

A1-tree-only returns on average 15.09 in B1, 16.04 in B2 leaves per question (scored as returned, not padded to 10); the pools hold 25 to 60 leaves.

## SHUFFLED-SUMMARIES negative control

On the 30 questions where it ran. The expectation about zero is not pre-registered for pools this small.
Lists returned empty: 0; failed recalls: 0; mean leaves returned: 15.87.

### A0, A1, A1-tree-only and SHUFFLED on those questions

n = 30 questions. Cells are mean [95% CI], percentile clustered bootstrap, primary cluster (conversation x session of first evidence), 10,000 resamples, seed 16096.

| Metric (n with the metric) | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only | SHUFFLED-SUMMARIES |
|---|---|---|---|---|
| gold-leaf recall@5 (n=30) | 0.694 [0.542, 0.833] | 0.850 [0.744, 0.943] | 0.900 [0.811, 0.970] | 0.144 [0.036, 0.260] |
| gold-leaf recall@10 (n=30) | 0.772 [0.631, 0.896] | 0.933 [0.851, 0.989] | 0.944 [0.862, 1.000] | 0.328 [0.183, 0.480] |
| gold-session recall@5 (n=30) | 0.761 [0.622, 0.887] | 0.894 [0.806, 0.968] | 0.911 [0.822, 0.978] | 0.200 [0.083, 0.323] |
| MRR@10 (leaf) (n=30) | 0.610 [0.464, 0.748] | 0.732 [0.606, 0.849] | 0.601 [0.488, 0.718] | 0.100 [0.043, 0.176] |
| chance reference, gold-leaf recall@5 / @10 | 0.122 / 0.244 | 0.122 / 0.244 | 0.122 / 0.244 | 0.122 / 0.244 |

Conversation-only clusters (10 clusters at most), the sensitivity interval:

| Metric | A0 (flat) | A1 (tree + flat, RRF) | A1-tree-only | SHUFFLED-SUMMARIES |
|---|---|---|---|---|
| gold-leaf recall@5 | 0.694 [0.590, 0.797] | 0.850 [0.772, 0.924] | 0.900 [0.833, 0.957] | 0.144 [0.032, 0.273] |
| gold-leaf recall@10 | 0.772 [0.687, 0.859] | 0.933 [0.862, 0.988] | 0.944 [0.872, 1.000] | 0.328 [0.148, 0.495] |
| gold-session recall@5 | 0.761 [0.641, 0.882] | 0.894 [0.818, 0.963] | 0.911 [0.844, 0.970] | 0.200 [0.079, 0.322] |
| MRR@10 (leaf) | 0.610 [0.435, 0.768] | 0.732 [0.573, 0.874] | 0.601 [0.479, 0.715] | 0.100 [0.043, 0.164] |

Paired differences (mean of per-question differences, same clustering):

| Metric | A1 minus A0 | A1-tree-only minus A0 | A1 minus A1-tree-only | A1-tree-only minus SHUFFLED |
|---|---|---|---|---|
| gold-leaf recall@5 | 0.156 [0.044, 0.289] (conv: [0.040, 0.276]) | 0.206 [0.073, 0.356] (conv: [0.078, 0.333]) | -0.050 [-0.135, 0.012] (conv: [-0.132, 0.012]) | 0.756 [0.622, 0.885] (conv: [0.618, 0.900]) |
| gold-leaf recall@10 | 0.161 [0.046, 0.298] (conv: [0.048, 0.274]) | 0.172 [0.057, 0.310] (conv: [0.062, 0.282]) | -0.011 [-0.036, 0.000] (conv: [-0.036, 0.000]) | 0.617 [0.462, 0.764] (conv: [0.473, 0.778]) |
| gold-session recall@5 | 0.133 [0.033, 0.258] (conv: [0.000, 0.258]) | 0.150 [0.038, 0.281] (conv: [0.021, 0.288]) | -0.017 [-0.063, 0.023] (conv: [-0.062, 0.023]) | 0.711 [0.533, 0.869] (conv: [0.548, 0.881]) |
| MRR@10 (leaf) | 0.122 [0.035, 0.216] (conv: [0.051, 0.219]) | -0.009 [-0.198, 0.180] (conv: [-0.210, 0.182]) | 0.131 [-0.027, 0.283] (conv: [-0.026, 0.301]) | 0.501 [0.388, 0.624] (conv: [0.370, 0.634]) |

### Cost and latency

Model calls charged (build plus recall, retries included): **3246**.

| Task | calls | schema valid | strict valid | throttled | input tokens | output tokens | latency median s | latency p90 s |
|---|---|---|---|---|---|---|---|---|
| build_l1 | 33 | 33 | 33 | 0 | 345216 | 65615 | 13.78 | 15.33 |
| organise | 1755 | 1754 | 1753 | 0 | 2417897 | 824336 | 4.44 | 7.81 |
| route | 1458 | 1458 | 758 | 0 | 4363233 | 287680 | 3.49 | 3.90 |

Schema valid means the reply parsed and matched the task's schema. Strict valid also applies the prompt's own rules (for route calls, each reason at most 12 words); the selections of a call that is schema valid but not strict valid are kept, as in S2.

Tree build: 10 conversations, 33 build calls, 176 s in total.

Per tree recall (wall seconds around the recall, which includes waiting for a concurrency slot; calls inside a recall are serial): median 32.48 s, mean 33.10 s, p90 41.97 s, min 9.33 s, max 54.65 s (n=400).
Calls per tree recall: median 7.00, mean 7.48, p90 9.00, min 4.00, max 9.00 (n=400).
Holder (route) calls per recall: median 3.00, mean 3.40, p90 4.00, min 2.00, max 4.00 (n=400).
Seconds the recall spent inside its calls: median 32.41 s, mean 33.02 s, p90 41.89 s, min 9.26 s, max 54.57 s (n=400).
SHUFFLED recall wall: median 31.22 s, mean 32.01 s, p90 40.03 s, min 15.81 s, max 41.46 s (n=30); calls: median 7.00, mean 7.40, p90 9.00, min 4.00, max 9.00 (n=30).
A0 (flat) per query, including the query embedding, all eligible questions: median 0.03 s, mean 0.03 s, p90 0.03 s, min 0.02 s, max 0.14 s (n=1536).

## Limits (fixed in the plan, section 10)

- Pools are 25 to 60 leaves: a top-10 list covers a large share of a pool, A0 is already high, and the tree has one level of 2 to 4 nodes. These numbers do not transfer to the 4,000 to 78,000 leaf pools of the LongMemEval work.
- Retrieval only: no answer generation and no end-to-end QA accuracy.
- Model: Sonnet through the Claude CLI at the version installed on the host; not the model of S3 or S6-H1.
- The session date is attached to the first leaf of a session only. Every figure is EXPLORATORY.

