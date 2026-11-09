"""Draw the 12 hour and 30 minute heart rate charts as one PNG."""

import io

import numpy as np
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

SURFACE = "#17181B"
LINE = "#E5484D"
GRID = "#2C2E33"
INK = "#F2F3F5"
MUTED = "#9A9DA5"

HOUR = 3600
MINUTE = 60
MIN_BPM = 45

# (seconds shown, seconds per averaged point, x ticks as (seconds ago, label))
WINDOWS = {
    "12 hours": (12 * HOUR, 2 * MINUTE, [(12 * HOUR, "12h"), (8 * HOUR, "8h"), (4 * HOUR, "4h"), (0, "now")]),
    "30 min": (30 * MINUTE, 15, [(30 * MINUTE, "30m"), (20 * MINUTE, "20m"), (10 * MINUTE, "10m"), (0, "now")]),
}
GAP_BINS = 4  # break the line after this many empty bins, so outages aren't drawn as data


def binned(samples: list[tuple[int, int]], now: float, span: int, bin_seconds: int) -> tuple[np.ndarray, np.ndarray]:
    """Average samples into bins. Returns seconds relative to now (negative) and BPM."""
    bins: dict[int, list[int]] = {}
    for measured_at, bpm in samples:
        if now - measured_at <= span:
            bins.setdefault(measured_at // bin_seconds, []).append(bpm)
    keys = sorted(bins)
    times = np.array([(k + 0.5) * bin_seconds - now for k in keys], dtype=float)
    return np.minimum(times, 0), np.array([sum(bins[k]) / len(bins[k]) for k in keys], dtype=float)


def with_gaps(times: np.ndarray, bpm: np.ndarray, max_gap: int) -> tuple[np.ndarray, np.ndarray]:
    """Insert NaNs where readings stopped, which breaks the line."""
    breaks = np.where(np.diff(times) > max_gap)[0] + 1
    return np.insert(times, breaks, np.nan), np.insert(bpm, breaks, np.nan)


def draw(ax, title: str, samples: list[tuple[int, int]], now: float, span: int, bin_seconds: int, ticks) -> None:
    times, bpm = binned(samples, now, span, bin_seconds)

    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0, labelsize=12, pad=5)
    ax.set_xlim(-span, 0)
    ax.set_xticks([-seconds for seconds, _ in ticks], [label for _, label in ticks])
    ax.get_xticklabels()[0].set_ha("left")
    ax.get_xticklabels()[-1].set_ha("right")
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", color=INK, fontsize=15, fontweight="bold", pad=12)

    if not len(bpm):
        ax.set_yticks([])
        ax.text(0.5, 0.5, "no data yet", transform=ax.transAxes, color=MUTED, fontsize=13, ha="center", va="center")
        return

    ax.set_title(f"{bpm.min():.0f}–{bpm.max():.0f} bpm", loc="right", color=MUTED, fontsize=12, pad=12)
    floor = min(MIN_BPM, bpm.min() - 6)  # only drops below MIN_BPM so a very low reading isn't cut off
    ax.set_ylim(floor, bpm.max() + 6)

    line_x, line_y = with_gaps(times, bpm, bin_seconds * GAP_BINS)
    ax.fill_between(line_x, line_y, floor, color=LINE, alpha=0.14, linewidth=0)
    ax.plot(line_x, line_y, color=LINE, linewidth=2.2, solid_capstyle="round", solid_joinstyle="round")
    ax.scatter(times[-1], bpm[-1], s=46, color=LINE, edgecolors=SURFACE, linewidths=1.6, zorder=3, clip_on=False)


def render_chart(samples: list[tuple[int, int]], now: float) -> bytes:
    """Render (measured_at_seconds, bpm) samples to PNG bytes."""
    figure = Figure(figsize=(7.6, 3), dpi=150, facecolor=SURFACE)
    axes = figure.subplots(1, 2, gridspec_kw={"wspace": 0.2})
    for ax, (title, (span, bin_seconds, ticks)) in zip(axes, WINDOWS.items()):
        draw(ax, title, samples, now, span, bin_seconds, ticks)
    figure.subplots_adjust(left=0.065, right=0.975, top=0.84, bottom=0.13)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=SURFACE)
    return buffer.getvalue()
