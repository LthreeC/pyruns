"""
Basic Example: Argparse Support
========================================

Showcases how Pyruns seamlessly picks up standard `argparse` arguments.
You do not need to import pyruns to use pyruns.

1. Run this file with python directly:
   `python main.py --lr 0.01 --epochs 50`

2. Or, run this file with pyruns to manage experiments:
   `pyr main.py`
"""
import pyruns
import argparse
import time

def main():
    parser = argparse.ArgumentParser(description="A simple ML training script.")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate")
    parser.add_argument("--epochs", type=int, default=10, help="Number of training epochs")
    parser.add_argument("-b", "--batch_size", type=int, default=32, help="Batch size for training")
    parser.add_argument("--optimizer", type=str, default="adam", choices=["adam", "sgd"], help="Optimizer choice")
    
    args = parser.parse_args()

    print(f"🚀 Starting training with {args.optimizer.upper()} optimizer!")
    print(f"Hyperparameters: LR={args.lr}, Batch Size={args.batch_size}")

    last_loss = 0

    for epoch in range(1, args.epochs + 1):
        time.sleep(0.5)  # Simulate compute
        loss = 1.0 / (epoch * args.lr * 100)
        last_loss = loss
        print(f"Epoch {epoch}/{args.epochs} - Loss: {loss:.4f}")
    
    pyruns.add_monitor(last_loss=last_loss)
    print("✅ Training complete.")

if __name__ == "__main__":
    main()
