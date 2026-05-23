# MF4 Standalone Decoder

Standalone tool to decode raw MF4 files (CAN / FlexRay / LIN frames) into
signal-level MF4 files, using the same decoding pipeline as the live
acquisition system.

## Quick Start

```bash
# 1. Create a virtual environment (optional but recommended)
python3 -m venv .venv
source .venv/bin/activate    # Linux/Mac
# .venv\Scripts\activate     # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Decode a raw MF4 file
python decode_mf4.py /path/to/raw_recording.mf4
```

## Output Location

By default the decoded file is written **next to the input file**, with
`_decoded` appended before the extension:

| Input path | Output path |
|---|---|
| `/data/logs/trip_001.mf4` | `/data/logs/trip_001_decoded.mf4` |
| `../recording.mf4` | `../recording_decoded.mf4` |

Use `--output-dir` to write the decoded file into a specific folder (the
filename stays `<input>_decoded.mf4`):

```bash
python decode_mf4.py /data/logs/trip_001.mf4 --output-dir /results
# → /results/trip_001_decoded.mf4
```

Or use `-o` / `--output` to set the full output path explicitly:

```bash
python decode_mf4.py /data/logs/trip_001.mf4 -o /results/trip_001_signals.mf4
```

## Usage

```
python decode_mf4.py <raw.mf4>                              # output next to input (*_decoded.mf4)
python decode_mf4.py <raw.mf4> --output-dir /results        # decoded file into /results/
python decode_mf4.py <raw.mf4> -o /path/to/decoded.mf4      # explicit output path
python decode_mf4.py <raw.mf4> --list-signals                # list all decodable signals
python decode_mf4.py <raw.mf4> --signals "ESP_21.ESP_v_Signal,Motor_12.Motor_Moment"
python decode_mf4.py <raw.mf4> --channel 1                   # decode only CAN ch 1
python decode_mf4.py <raw.mf4> --start 10 --end 60           # time window (seconds)
python decode_mf4.py <raw.mf4> --threads 8                   # use 8 parallel workers
python decode_mf4.py <raw.mf4> --threads 1                   # single-threaded decode
python decode_mf4.py <raw.mf4> --no-cache                    # force re-parse of databases
```

### Parallel Decoding

Decoding runs in **parallel** by default using multiple worker processes
(default: **5**).  The raw frame table is split into equal-sized chunks,
each worker decodes its chunk independently via `multiprocessing` (fork),
and results are merged back in order.

| Flag | Default | Description |
|---|---|---|
| `--threads N` | `5` | Number of parallel worker processes |
| `--threads 1` | — | Disable parallelism (single-threaded decode) |

Set `--threads` to roughly the number of CPU cores available for best
throughput.  Using `--threads 1` is useful for debugging or when running on
a constrained system.

### Database Cache

Parsed database files (DBC, ARXML, FIBEX) are automatically cached as a
pickle file under `.cache/` the first time they are loaded.  Subsequent
runs skip the expensive XML/DBC parsing and load the cache instead
(~0.6 s vs ~30 s).

The cache is keyed by the set of database file paths, sizes, and
modification timestamps — it is automatically invalidated when any source
file changes.

| Flag | Description |
|---|---|
| `--no-cache` | Force a fresh parse, deleting the existing cache file |

### Custom Databases

By default the script auto-discovers `.dbc`, `.arxml`, and `.fibex`/`.xml`
files in the `databases/` sub-folders.  Override with CLI flags:

```
python decode_mf4.py raw.mf4 --dbc my.dbc --arxml my.arxml --fibex my.xml
```

## Decode Cascade

For each raw frame, decoders are tried in this order:

1. **DBC** (via `cantools`) — CAN / CAN-FD
2. **ARXML** (AUTOSAR catalogue) — CAN / FlexRay / LIN, with COMPU-METHOD
3. **FIBEX** (FlexRay KMatrix XML) — FlexRay slot-level decode

## Directory Structure

```
mf4_standalone_decoder/
├── decode_mf4.py          # Main script (entry point)
├── dbc_loader.py          # DBC parser / decoder
├── arxml_parser.py        # AUTOSAR ARXML catalogue parser
├── arxml_decoder.py       # Frame decoder using ARXML catalogue
├── fibex_loader.py        # FIBEX / FlexRay KMatrix XML parser
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── .cache/                # Auto-generated database pickle cache
└── databases/
    ├── dbc/               # CAN DBC files
    ├── arxml/             # AUTOSAR ARXML files
    └── fibex/             # FlexRay FIBEX / KMatrix XML files
```

## Requirements

- Python 3.10+
- See `requirements.txt`
