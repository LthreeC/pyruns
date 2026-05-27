import argparse
import random
import time

import pyruns


def main() -> None:
    parser = argparse.ArgumentParser(description="Launcher train entrypoint")
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument("--model", choices=["tiny", "small"], default="tiny")
    args = parser.parse_args()

    random.seed(args.seed)
    loss = 1.0
    for epoch in range(1, args.epochs + 1):
        time.sleep(0.05)
        loss = round(loss * (0.78 + random.random() * 0.03), 6)
        print(f"train epoch={epoch} model={args.model} lr={args.lr} loss={loss}")
        pyruns.track(loss=loss)
    pyruns.record(final_loss=loss, model=args.model, seed=args.seed)


if __name__ == "__main__":
    main()
