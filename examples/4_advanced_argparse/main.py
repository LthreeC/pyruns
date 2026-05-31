import argparse
import json
import os
import random
import time

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

import pyruns


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advanced Pyruns argparse example")
    parser.add_argument("dataset", nargs="?", default="toy", help="Dataset name")
    parser.add_argument("--layers", nargs="+", type=int, default=[64, 64], help="Hidden layer sizes")
    parser.add_argument("--tag", action="append", default=[], help="Repeatable experiment tag")
    parser.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--use-amp", action="store_true", default=False)
    parser.add_argument("--no-cache", dest="cache", action="store_false", default=True)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--dropout", type=float, default=-1.0)
    parser.add_argument("--device", choices=["cpu", "cuda", "mps"], default="cpu")
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    random.seed(args.seed)

    summary = {
        "dataset": args.dataset,
        "layers": args.layers,
        "tags": args.tag,
        "compile": args.compile,
        "use_amp": args.use_amp,
        "cache": args.cache,
        "verbose": args.verbose,
        "dropout": args.dropout,
        "device": args.device,
        "seed": args.seed,
        "env_marker": os.environ.get("PYRUNS_EXAMPLE_ENV", ""),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    width = max(1, sum(args.layers))
    loss = 1.0
    for step in tqdm(range(1, 4), ascii=True, unit="step"):
        time.sleep(0.005)
        loss = round(loss * (0.72 + random.random() * 0.04), 6)
        throughput = round(width / step, 3)
        print(f"step={step} loss={loss} throughput={throughput}")
        pyruns.track(loss=loss, throughput=throughput)

    pyruns.record(
        final_loss=loss,
        layer_count=len(args.layers),
        compile=args.compile,
        cache=args.cache,
        verbose=args.verbose,
        env_marker=summary["env_marker"],
    )

    artifact_dir = pyruns.artifact_dir()
    summary_path = os.path.join(artifact_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "run_index": pyruns.get_run_index(),
                "final_loss": loss,
                "env_marker": summary["env_marker"],
            },
            f,
            indent=2,
            sort_keys=True,
        )


if __name__ == "__main__":
    main()
