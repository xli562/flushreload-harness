# Flush+Reload accuracy vs VCPU load


## Prerequisites

The experiment requires at least two vCPUs. The loader is pinned to CPU 0 and calibration/`newspy` are pinned to CPU 1, so cache noise from the loader is concurrent with the attack on the other vCPU. `calibration.c`, `newspy.cpp`, and `loadgen.c` must all be present in `~/Flush-Reload` on the VM.

## 1. Setup for a Virtual Machine (virtualbox/anything) 
Full sequence run on the VM to get OpenSSL built and the T-table offsets found, before any of the harness stuff below.

```bash
# get openssl (repo tested on 1.1.0f) from https://openssl-library.org/source/old/1.1.0/
wget https://github.com/openssl/openssl/releases/download/OpenSSL_1_1_0f/openssl-1.1.0f.tar.gz

# extract
tar -xvf openssl-1.1.0f.tar.gz
cd openssl-1.1.0f

# configuration: t-table C implementation (no-asm), debug symbols (-d) to locate the T-tables later, shared object, no hardware AES engine
./config -d shared no-asm no-hw

# run the following if you experience the <"glob" is not exported by the File::Glob module> error when running ./config
grep -rn "File::Glob" .
grep -rl "File::Glob" . | xargs sed -i \
  -e 's/qw\/glob\//qw\/:glob\//g' \
  -e 's/qw(glob)/qw(:glob)/g' \
  -e 's/qw\[glob\]/qw[:glob]/g'

# build
sudo make
sudo make install_sw

# OpenSSL installs to /usr/local by default, not a system dir the linker searches for
export LD_LIBRARY_PATH=/usr/local/lib

# find Te0-Te3 offsets in the actual built libcrypto.so -- used to populate the `probe[]` array in newspy.cpp and PROBE_OFFSETS in loadgen.c
cd /usr/local/lib
readelf -a libcrypto.so > ~/aeslib.txt
grep -i "Te0\|Te1\|Te2\|Te3" ~/aeslib.txt
```

If `$HOME/opt/openssl-1.1.0f/lib` (the path the harness's `loadgen.c` / `vm/newspy.cpp` expect) doesn't exist on a given VM but the real build is at `/usr/local/lib` as above, symlink it rather than editing the hardcoded paths:

```bash
mkdir -p ~/opt/openssl-1.1.0f
ln -s /usr/local/lib ~/opt/openssl-1.1.0f/lib
```

### 1.a. [Test] Standalone calibrattion and Flush-Reload
```bash
# find the cache hit/miss threshold for this machine
cd ~/Flush-Reload/vm/
g++ calibration.c -o calibration
./calibration
# put "Suggested cache hit/miss threshold" into MIN_CACHE_MISS_CYCLES (optional, this version of newspy.cpp takes in a -c argument for the threshold)

# build and run the actual attack
g++ newspy.cpp -o newspy -I/usr/local/include/ssl -L/usr/local/lib -lcrypto
./newspy
```

### 1.b Run Harness
```bash
# compile everything in vm/
cd ~/Flush-Reload/vm/
gcc -O2 -o loadgen loadgen.c && gcc -O2 -o calibration calibration.c && g++ -O2 -o newspy newspy.cpp -I/usr/local/include/openssl -L$HOME/opt/openssl-1.1.0f/lib -Wl,-rpath,$HOME/opt/openssl-1.1.0f/lib -lcrypto

# run
python3 run_experiments_vm.py both --mode mem
python3 run_experiments_vm.py both --mode evict --levels 0 10 20 30 40 50 60 70 80 90 100

# or, if binaries live elsewhere
python3 run_experiments_vm.py both --mode mem --bin-dir ~/Flush-Reload

```

Results, files, and formats discussed in the Results section below

## 2. Setup for WSL

These were run from the repository root on WSL. `ssh.exe` and `scp.exe` are the Windows OpenSSH binaries; `flrl` is the VM SSH alias.

```bash
# compile
scp.exe vm/loadgen.c vm/newspy.cpp vm/calibration.c flrl:Flush-Reload/
ssh.exe flrl 'cd ~/Flush-Reload && gcc -O2 -o loadgen loadgen.c && gcc -O2 -o calibration calibration.c && g++ -O2 -o newspy newspy.cpp -I/usr/local/include/openssl -L$HOME/opt/openssl-1.1.0f/lib -Wl,-rpath,$HOME/opt/openssl-1.1.0f/lib -lcrypto && echo BUILD_OK'

# run
python3 run_experiments_wsl.py both --mode mem
python3 run_experiments_wsl.py both --mode evict --levels 0 10 20 30 40 50 60 70 80 90 100
```
Note: do **not** pass `-fsanitize=address` — it distorts `rdtsc` timing and has previously caused `mmap` failures.

## Load generation model

`loadgen {mem|busy|target|evict} DUTY` runs a 20 ms duty cycle: `DUTY`% work, rest `nanosleep`. `mem` streams writes through a 256 MB buffer (larger than the 72 MB LLC), `busy` runs a short ALU loop, `target` touches the four OpenSSL table lines newspy watches, and `evict` flushes those same four lines with `clflushopt`. Actual load is measured from `/proc/<pid>/stat` before newspy runs, over an approximately 3 s window using actual elapsed VM uptime. `load_pct` is the utilization of the loader's pinned vCPU, so it remains on a 0–100% scale.

The loader remains running during calibration and `newspy`, and is terminated only after `newspy` exits or the remote script is interrupted. Each `calibration` run is capped at 60 s and each `newspy` run at 300 s; a timeout aborts the run with a non-zero exit.

## Experiment semantics

- **exp1** — threshold `T0` from 10 zero-load calibrations, then used at every level. Shows how a threshold that is correct at 0 % load degrades under load.
- **exp2** — 10 calibration runs at each level, whose mean feeds newspy. Shows whether recalibrating recovers accuracy.

## Results

To run one experiment at a time:
```bash
python run_experiments_{}.py exp1 --levels 0 30 60 90 --reps 5
python run_experiments_{}.py exp2 --mode busy
```

Outputs go to `results/`:

| File | Contents |
|---|---|
| `<stamp>_exp1_<mode>.csv` | Retained exp1 results for that run |
| `<stamp>_exp2_<mode>.csv` | Retained exp2 results for that run |
| `exp1_<mode>.csv`, `exp2_<mode>.csv` | Latest results for each experiment/mode pair |



## Recorded results

Single run reference results for `mem` and `evict` stored in `results/`

### Plotting script

The plotting script `plot_results.py` only expects one set of results (exp1 and 2, for `mem` and `evict`), and places the generated results in the results directory.