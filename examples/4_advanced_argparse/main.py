import argparse
import json
import os
import random
import time

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
    for step in range(1, 4):
        time.sleep(0.05)
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


if __name__ == "__main__":
    main()
