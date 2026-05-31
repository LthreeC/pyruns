import math
import os
import random
import time

import pyruns


def main() -> None:
    cfg = pyruns.load()
    seed = int(cfg.training.seed)
    random.seed(seed)

    model_name = str(cfg.model.name)
    width = int(cfg.model.encoder.blocks.width)
    depth = int(cfg.model.encoder.blocks.depth)
    dropout = float(cfg.model.encoder.activation.dropout)
    lr = float(cfg.training.optimizer.params.lr)
    weight_decay = float(cfg.training.optimizer.params.weight_decay)
    batch_size = int(cfg.training.batch.size)
    epochs = int(cfg.training.epochs)
    tokenizer = str(cfg.dataset.preprocessing.tokenizer.name)
    lowercase = bool(cfg.dataset.preprocessing.tokenizer.options.lowercase)
    noise_std = float(cfg.dataset.preprocessing.augmentation.policy.noise.std)
    temperature = float(cfg.model.head.calibration.temperature)
    precision = str(cfg.runtime.precision)
    compile_enabled = bool(cfg.runtime.compile.enabled)

    print(f"experiment={cfg.experiment.name}")
    print(f"dataset={cfg.dataset.name} tokenizer={tokenizer} lowercase={lowercase}")
    print(f"model={model_name} width={width} depth={depth} dropout={dropout}")
    print(f"lr={lr} batch_size={batch_size} precision={precision}")
    print(f"env_marker={os.environ.get('PYRUNS_EXAMPLE_ENV', '')}")

    final_loss = 0.0
    final_acc = 0.0
    capacity = max(1, width * depth)
    regularizer = dropout + noise_std + weight_decay
    for epoch in range(1, epochs + 1):
        time.sleep(0.05)
        final_loss = round(
            (1.0 + regularizer) / (math.sqrt(epoch + lr * 1000) + math.log2(capacity) / 10),
            6,
        )
        final_acc = round(
            min(0.99, 0.52 + epoch * 0.05 + math.log2(width) / 200 + random.random() * 0.01),
            6,
        )
        print(f"epoch={epoch} loss={final_loss} acc={final_acc}")
        pyruns.track(loss=final_loss, acc=final_acc, lr=lr)

    pyruns.record(
        final_loss=final_loss,
        final_acc=final_acc,
        seed=seed,
        model=model_name,
        width=width,
        depth=depth,
        tokenizer=tokenizer,
        lowercase=lowercase,
        batch_size=batch_size,
        noise_std=noise_std,
        temperature=temperature,
        precision=precision,
        compile_enabled=compile_enabled,
        env_marker=os.environ.get("PYRUNS_EXAMPLE_ENV", ""),
    )


if __name__ == "__main__":
    main()
