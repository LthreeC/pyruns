"""
Basic Example: Pyruns Config Support
========================================

Showcases how Pyruns can provide structured configurations straight into your script.

1. First launch the UI:
   `pyr main.py`
   
2. Notice how Pyruns detects `pyruns.load()` and builds an appropriate task runner for you.
"""

import time

try:
    import pyruns
except ImportError:
    print("Please install pyruns to run this example: `pip install pyruns`")
    exit(1)


def main():
    # If run through Pyruns, `load()` automatically detects the config yaml file!
    # If starting directly (e.g. `python main.py`), you can optionally use `pyruns.read("config.yaml")` first.
    config = pyruns.load()
    print(config)

    # The config behaves like an object, allowing dot access to yaml values.
    # We will simulate reading hyperparameters if they are set, else use defaults.
    
    lr = config.lr
    epochs = config.epochs
    optimizer = config.optimizer
    
    print(f"🚀 Starting training with {optimizer.upper()} optimizer!")
    print(f"Hyperparameters: LR={lr}")

    for epoch in range(1, epochs + 1):
        time.sleep(0.5)  # Simulate compute
        loss = 1.0 / (epoch * lr * 100)
        print(f"Epoch {epoch}/{epochs} - Loss: {loss:.4f}")
    
    print("✅ Training complete.")

if __name__ == "__main__":
    main()
