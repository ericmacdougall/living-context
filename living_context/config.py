"""Typed configuration. Every ASSUMPTION constant of docs/DESIGN.md lives here.

Labels follow DESIGN section 0. A value labelled ASSUMPTION can be changed
without changing the method. Nothing secret is held here: the file named by
the environment variable LC_CONFIG supplies paths and endpoints (placeholders
only in config.example.toml), and secrets are read from 0600 files by path.
"""
from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


@dataclass(frozen=True)
class Constants:
    # Leaves and tree shape (DESIGN 5.2, 5.4)
    leaf_cap: int = 1536            # ASSUMPTION: max tokens per leaf
    f_target: int = 16              # ASSUMPTION: children per node, target
    f_max: int = 24                 # ASSUMPTION: children per node, max
    f_min: int = 4                  # ASSUMPTION: children per node, min
    m_max: int = 2                  # ASSUMPTION: parents per leaf in a family
    h_ctx: int = 12_000             # ASSUMPTION: holder context budget, tokens
    chars_per_token_bound: float = 2.4  # ASSUMPTION: conservative chars per token
    title_terms: int = 4            # ASSUMPTION: tf-idf terms in a code-built title
    # Fan-out budget (DESIGN 8.2)
    b_holders_extra: int = 36       # ASSUMPTION: B_HOLDERS = top holders + this
    b_fanout: int = 6               # ASSUMPTION: children named per holder
    b_organise: int = 12            # ASSUMPTION: organise calls per recall
    reason_max_words: int = 12      # ASSUMPTION
    reason_ngram: int = 5           # ASSUMPTION
    holder_timeout_s: int = 45      # ASSUMPTION
    # Admission and availability (DESIGN 7.1, 15)
    max_line_bytes: int = 1 << 20   # ASSUMPTION: MAX_LINE = 1 MiB
    write_timeout_s: int = 30       # ASSUMPTION
    send_buffer_bytes: int = 64 * 1024   # ASSUMPTION: bound on kernel-held bytes per connection
    # Revocation journal (DESIGN 4.4)
    j_size: int = 4 << 20           # ASSUMPTION: J_SIZE, each of journal and spare
    n_principals: int = 10_000      # ASSUMPTION
    d_fsync_s: float = 2.0          # ASSUMPTION: D_FSYNC
    key_expiry_days: int = 7        # ASSUMPTION: API key expiry
    # Erasure and retention (DESIGN 14)
    b_erase_s: int = 60             # ASSUMPTION: B_ERASE
    p_purge_s: int = 3600           # ASSUMPTION: P_PURGE
    r_keys_days: int = 35           # ASSUMPTION: R_KEYS
    r_logs_days: int = 7            # ASSUMPTION: R_LOGS
    r_gen_count: int = 5            # ASSUMPTION: R_GEN generations ...
    r_gen_days: int = 14            # ASSUMPTION: ... or days, whichever is longer
    d_wp_s: int = 600               # ASSUMPTION: D_WP
    # Fusion and expansion (DESIGN 8.4 to 8.6)
    rrf_k: int = 60                 # SOURCE: Elasticsearch RRF default
    rrf_window: int = 50            # SOURCE: Elasticsearch RRF default
    expand_hops: int = 2            # ASSUMPTION
    expand_neighbours: int = 10     # ASSUMPTION
    expand_leaves: int = 50         # ASSUMPTION
    # Feedback (DESIGN 9 to 13)
    exploration_share: float = 0.1  # ASSUMPTION: 1 slot in 10
    ips_clip: float = 0.05          # ASSUMPTION
    n_audit: int = 100              # ASSUMPTION
    k_cand: int = 5                 # ASSUMPTION
    s_min: int = 2                  # ASSUMPTION
    miss_rate_min: float = 0.5      # ASSUMPTION
    gate_alpha: float = 0.05        # ASSUMPTION
    gate_min_clusters: int = 8       # ASSUMPTION
    gate_max_queries: int = 60      # ASSUMPTION
    gate_cluster_max: int = 6       # ASSUMPTION
    # Provisioning (DESIGN 6.4)
    shard_budget_fraction: float = 0.8   # ASSUMPTION
    max_classes: int = 4            # ASSUMPTION
    embed_dims: int = 384           # SOURCE: multilingual-e5-small
    embed_window_tokens: int = 480  # ASSUMPTION


@dataclass(frozen=True)
class Settings:
    """Paths and endpoints from the LC_CONFIG file; no secret values."""
    es_url: str = "https://127.0.0.1:9200"
    es_ca_path: str = ""
    key_dir: str = ""           # directory of 0600 key files, read by path
    data_root: str = ""         # keystore, journals and logs live under it
    dataset_path: str = ""
    constants: Constants = field(default_factory=Constants)


def load(path: str | None = None) -> Settings:
    """Load settings from `path` or from the file named by LC_CONFIG."""
    path = path or os.environ.get("LC_CONFIG")
    if not path:
        return Settings()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    consts = raw.pop("constants", {})
    known = {f_.name for f_ in fields(Constants)}
    unknown = set(consts) - known
    if unknown:
        raise ValueError(f"unknown constants: {sorted(unknown)}")
    sknown = {f_.name for f_ in fields(Settings)} - {"constants"}
    unknown = set(raw) - sknown
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    return Settings(constants=replace(Constants(), **consts), **raw)


def read_secret_file(path: str) -> str:
    """Read a secret from a file that only its owner can access (LAB-R7)."""
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError("secret file must be mode 0600")
    return Path(path).read_text().strip()
