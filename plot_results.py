import glob
import pandas as pd
import matplotlib.pyplot as plt

paths = sorted(glob.glob("results/*_exp[12]_*.csv"))

COLORS = {0: "tab:blue", 1: "tab:orange"}

TITLES = {
    "mem": "Uniform Cache Pressure",               # mem: streams writes through a 256MB buffer (> LLC), evicts indiscriminately
    "evict": "Targeted Eviction of Probed Lines",  # evict: clflushopt's exactly the four lines newspy watches
}

for mode in ["mem", "evict"]:
    fig, (acc_ax, thresh_ax) = plt.subplots(1, 2, figsize=(9, 4))
    for path in paths:
        df = pd.read_csv(path)
        if df["mode"].iloc[0] != mode:
            continue
        exp = 0 if "exp1" in path else 1
        color = COLORS[exp]

        avg = df.groupby("level")["accuracy"].mean()
        acc_ax.plot(avg.index, avg.values, marker="o", color=color, label=f"exp{exp+1}")

        if exp == 0:
            # exp1 uses the same fixed T0 at every level -- draw it as one line.
            thresh_ax.axhline(df["threshold_used"].iloc[0], linestyle="--",
                               color=color, label="exp1 (fixed T0)")
        else:
            # exp2 recalibrates per level -- average the recalibrated
            # threshold over reps at each level.
            thresh_avg = df.groupby("level")["threshold_mean"].mean()
            thresh_ax.plot(thresh_avg.index, thresh_avg.values, marker="o",
                            color=color, label="exp2 (recalibrated)")

    acc_ax.set_title(TITLES[mode])
    acc_ax.set_xlabel("Load level (%)")
    acc_ax.set_ylabel("Average success (accuracy)")
    acc_ax.legend()

    thresh_ax.set_title("threshold")
    thresh_ax.set_xlabel("Load level (%)")
    thresh_ax.set_ylabel("Cache hit/miss threshold")
    thresh_ax.legend()

    plt.tight_layout()
    plt.savefig(f"results/{mode}_plot.png")

plt.show()
