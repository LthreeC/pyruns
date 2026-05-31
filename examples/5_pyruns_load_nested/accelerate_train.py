"""Nested pyruns.load() example with optional automatic Accelerate launch.

Launch controls stay in environment variables:
  CUDA_VISIBLE_DEVICES=0,1
  ACCEL_NPROC=2
  ACCEL_MP=bf16
  ACCEL_PORT=29501
  ACCEL_OFF=1
  ACCEL_DEBUG=1

Training parameters live in YAML and are read with pyruns.load().
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any


def auto_accelerate() -> None:
    """Re-launch with accelerate on multi-GPU machines when available."""

    if (
        os.getenv("ACCEL_OFF")
        or os.getenv("RUNPY_ACCELERATED")
        or os.getenv("LOCAL_RANK")
        or os.getenv("RANK")
        or os.getenv("WORLD_SIZE")
        or "-h" in sys.argv
        or "--help" in sys.argv
    ):
        return

    try:
        import torch  # type: ignore
        import accelerate  # noqa: F401

        if not torch.cuda.is_available():
            return
        nproc = int(os.getenv("ACCEL_NPROC") or torch.cuda.device_count())
    except Exception:
        return

    if nproc <= 1:
        return

    cmd = [
        sys.executable,
        "-m",
        "accelerate.commands.launch",
        "--multi_gpu",
        "--num_processes",
        str(nproc),
    ]

    if os.getenv("ACCEL_MP"):
        cmd += ["--mixed_precision", os.environ["ACCEL_MP"]]
    if os.getenv("ACCEL_PORT"):
        cmd += ["--main_process_port", os.environ["ACCEL_PORT"]]

    cmd += [os.path.abspath(__file__), *sys.argv[1:]]

    if os.getenv("ACCEL_DEBUG"):
        print("[launch]", " ".join(cmd), flush=True)

    os.execvpe(
        sys.executable,
        cmd,
        {**os.environ, "RUNPY_ACCELERATED": "1"},
    )


auto_accelerate()

import pyruns  # noqa: E402


def _read_params(cfg: Any) -> dict[str, Any]:
    return {
        "seed": int(cfg.training.seed),
        "epochs": int(cfg.training.schedule.epochs),
        "lr": float(cfg.training.optimizer.params.lr),
        "weight_decay": float(cfg.training.optimizer.params.weight_decay),
        "grad_accum": int(cfg.training.batch.gradient_accumulation),
        "batch_size": int(cfg.training.batch.per_device),
        "num_workers": int(cfg.data.loader.num_workers),
        "pin_memory": bool(cfg.data.loader.pin_memory),
        "samples": int(cfg.data.synthetic.samples),
        "features": int(cfg.data.synthetic.features),
        "target_dim": int(cfg.data.synthetic.target_dim),
        "model_width": int(cfg.model.mlp.hidden.width),
        "activation": str(cfg.model.mlp.hidden.activation),
        "output_dir": str(cfg.experiment.output.dir),
        "save_model": bool(cfg.experiment.output.save_model),
    }


def _train_with_python(params: dict[str, Any]) -> tuple[str, float, bool]:
    random.seed(params["seed"])
    final_loss = 0.0
    scale = max(1.0, params["model_width"] / max(1, params["features"]))
    for epoch in range(1, params["epochs"] + 1):
        time.sleep(0.03)
        noise = random.random() * 0.01
        final_loss = round(
            (1.0 + params["weight_decay"] + noise)
            / (math.sqrt(epoch + params["lr"] * 10000) + math.log2(scale + 1)),
            6,
        )
        print(f"epoch={epoch}, loss={final_loss:.6f}, backend=python_fallback")
        pyruns.track(loss=final_loss, lr=params["lr"])
    return "python_fallback", final_loss, True


def _train_with_torch(params: dict[str, Any]) -> tuple[str, float, bool]:
    try:
        import torch
        from accelerate import Accelerator
        from accelerate.utils import set_seed
        from torch import nn
        from torch.utils.data import DataLoader, TensorDataset
    except Exception:
        return _train_with_python(params)

    accelerator = Accelerator(gradient_accumulation_steps=params["grad_accum"])
    set_seed(params["seed"])

    generator = torch.Generator().manual_seed(params["seed"])
    x = torch.randn(params["samples"], params["features"], generator=generator)
    y = torch.randn(params["samples"], params["target_dim"], generator=generator)

    loader = DataLoader(
        TensorDataset(x, y),
        batch_size=params["batch_size"],
        shuffle=True,
        num_workers=params["num_workers"],
        pin_memory=params["pin_memory"],
    )

    activation = nn.GELU() if params["activation"].lower() == "gelu" else nn.ReLU()
    model = nn.Sequential(
        nn.Linear(params["features"], params["model_width"]),
        activation,
        nn.Linear(params["model_width"], params["target_dim"]),
    )
    opt = torch.optim.AdamW(
        model.parameters(),
        lr=params["lr"],
        weight_decay=params["weight_decay"],
    )
    loss_fn = nn.MSELoss()
    model, opt, loader = accelerator.prepare(model, opt, loader)

    final_loss = 0.0
    for epoch in range(1, params["epochs"] + 1):
        model.train()
        for bx, by in loader:
            with accelerator.accumulate(model):
                loss = loss_fn(model(bx), by)
                accelerator.backward(loss)
                opt.step()
                opt.zero_grad(set_to_none=True)
        final_loss = round(float(loss.detach().cpu().item()), 6)
        accelerator.print(f"epoch={epoch}, loss={final_loss:.6f}, backend=torch_accelerate")
        if accelerator.is_main_process:
            pyruns.track(loss=final_loss, lr=params["lr"])

    accelerator.wait_for_everyone()
    if accelerator.is_main_process and params["save_model"]:
        output_dir = Path(params["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        torch.save(accelerator.unwrap_model(model).state_dict(), output_dir / "model.pt")

    return "torch_accelerate", final_loss, bool(accelerator.is_main_process)


def main() -> None:
    cfg = pyruns.load()
    params = _read_params(cfg)

    backend, final_loss, is_main_process = _train_with_torch(params)
    if not is_main_process:
        return

    summary = {
        "backend": backend,
        "final_loss": final_loss,
        "seed": params["seed"],
        "samples": params["samples"],
        "model_width": params["model_width"],
        "batch_size_per_device": params["batch_size"],
        "gradient_accumulation": params["grad_accum"],
        "mixed_precision_env": os.getenv("ACCEL_MP", ""),
        "port_env": os.getenv("ACCEL_PORT", ""),
        "env_marker": os.getenv("PYRUNS_EXAMPLE_ENV", ""),
    }

    artifact_path = Path(pyruns.artifact_dir()) / "summary.json"
    artifact_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    pyruns.record(summary)


if __name__ == "__main__":
    main()
