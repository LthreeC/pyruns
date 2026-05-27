import math
import os
import random
import time

import pyruns


def main() -> None:
    cfg = pyruns.load()
    seed = int(cfg.training.seed)
    random.seed(seed)

    print(f"experiment={cfg.experiment.name}")
    print(f"model={cfg.model.name} width={cfg.model.width} depth={cfg.model.depth}")
    print(f"lr={cfg.training.lr} batch_size={cfg.training.batch_size}")
    print(f"env_marker={os.environ.get('PYRUNS_EXAMPLE_ENV', '')}")

    final_loss = 0.0
    final_acc = 0.0
    for epoch in range(1, int(cfg.training.epochs) + 1):
        time.sleep(0.05)
        final_loss = round(1.0 / math.sqrt(epoch + float(cfg.training.lr) * 1000), 6)
        final_acc = round(0.5 + epoch * 0.06 + random.random() * 0.01, 6)
        print(f"epoch={epoch} loss={final_loss} acc={final_acc}")
        pyruns.track(loss=final_loss, acc=final_acc)

    pyruns.record(
        final_loss=final_loss,
        final_acc=final_acc,
        seed=seed,
        model=cfg.model.name,
        env_marker=os.environ.get("PYRUNS_EXAMPLE_ENV", ""),
    )


if __name__ == "__main__":
    main()
