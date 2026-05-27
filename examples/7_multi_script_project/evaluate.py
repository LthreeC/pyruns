import time

import pyruns


def main() -> None:
    cfg = pyruns.load()
    print(f"evaluate checkpoint={cfg.checkpoint}")
    print(f"dataset={cfg.dataset.name} split={cfg.dataset.split}")
    score = 0.0
    for step in range(1, int(cfg.eval.steps) + 1):
        time.sleep(0.05)
        score = round(0.6 + step * 0.03, 6)
        print(f"eval step={step} score={score}")
        pyruns.track(score=score)
    pyruns.record(score=score, split=cfg.dataset.split)


if __name__ == "__main__":
    main()
