# HTTP client performance benchmark

This benchmark compares the Python CLI with minimal `httpx`, `curl_cffi`, `pycurl`, `niquests`, and
Rust `reqwest` clients using the same five GitHub REST reads for one repository and cutoff. The
prototypes intentionally implement only the first page needed by the default `--limit 30`
benchmark. They are not replacement CLIs.

Build and run the alternating benchmark:

```bash
cargo build --release --manifest-path benchmarks/http-client-benchmark/Cargo.toml
uv --no-config run --locked --with 'httpx[http2]==0.28.1' \
  --with 'curl-cffi==0.16.0' --with 'pycurl==7.47.0' \
  --with 'niquests==3.21.0' \
  python benchmarks/http-client-benchmark/benchmark.py
```

The benchmark alternates execution order and reports how many paired runs returned the same category
counts. Counts can differ when GitHub activity arrives between the two requests.

## Fedora benchmark

Measured on 2026-08-20 against `getzola/zola` with a two-hour cutoff, two warmups, and ten
alternating measured runs per implementation:

| trial | Python median | Rust median | speedup | count matches |
|---|---:|---:|---:|---:|
| 1 | 0.885s | 0.710s | 1.25x | 10/10 |
| 2 | 0.854s | 0.660s | 1.29x | 10/10 |

The release binary is 7.9 MB. This is an upper-bound comparison for a rewrite because the prototype
returns only counts, implements only the first page, and combines Rust runtime differences with
`reqwest` connection management. The measured gain is useful but not large enough by itself to
justify maintaining a second implementation.

## Python HTTP client comparison

Two additional ten-run trials compared the standard-library implementation with `httpx.AsyncClient`
using HTTP/2. The second trial constrained `httpx` to one connection so all requests multiplex over
the same connection.

| transport | Python `urllib` | Python `httpx` | Rust `reqwest` | `httpx` speedup |
|---|---:|---:|---:|---:|
| default client limits | 0.833s | 0.814s | 0.627s | 1.02x |
| one HTTP/2 connection | 0.874s | 0.818s | 0.657s | 1.07x |

Counts matched in all 20 paired transport runs. The 19-56ms `httpx` improvement does not justify
adding the runtime dependency to `ghp`.

## Broader Python client comparison

Two promotion trials then compared the native and HTTP/2-capable candidates. A transport needed to
improve the Python CLI median by at least 150ms before considering a production integration.

| trial | `urllib` | `httpx` | `curl_cffi` | `pycurl` | Rust |
|---|---:|---:|---:|---:|---:|
| 1 | 0.878s | 0.779s | 0.712s | 0.684s | 0.631s |
| 2 | 0.840s | 0.756s | 0.789s | 1.654s | 0.653s |

All category counts matched in all 20 rounds. `curl_cffi` cleared the threshold once by 166ms, but
the repeat reduced the gain to 51ms. `pycurl` was highly unstable and slower than the current client
in the repeat. `httpx` was stable but stayed below the threshold. `curl_cffi` installs four runtime
distributions in total and its own package occupies about 37.5 MiB on this machine. `pycurl` is a
smaller dependency, but its multi interface makes the request code substantially harder to read.

`niquests` was tested separately as the remaining client with automatic HTTP/2 multiplexing. In a
two-run discovery check it took 0.923s versus 0.839s for the current client, with matching counts in
both rounds. That failed counterfactual did not warrant a promotion trial.

The result does not support changing the production HTTP library. Network and GitHub response time
dominate this one-shot CLI, so general library throughput benchmarks do not translate into a stable
win here. Rust remains the only repeatable material improvement, and its roughly 0.19-0.25s gain is
still too small to justify a rewrite by itself.
