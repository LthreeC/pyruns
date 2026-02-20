"""
Advanced Example: Metrics Logging
============================================

Showcases how to integrate pyruns metrics logging.
When you use `pyruns.add_monitor()`, the variables are tracked 
and safely stored in JSON for bulk CSV report exportation later.

Run with: `pyr train.py`
"""

import argparse
import time
import random

try:
    import pyruns
except ImportError:
    print("Please install pyruns to run this example: `pip install pyruns`")
    exit(1)


def train():
    parser = argparse.ArgumentParser()
    parser.add_argument("--learning_rate", type=float, default=3e-4)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--model", type=str, default="resnet18")
    
    args = parser.parse_args()

    print(f"Initializing {args.model} with dropout {args.dropout}...")
    
    current_loss = 2.5
    current_acc = 10.0

    for epoch in range(1, args.epochs + 1):
        # Simulate an epoch of training
        time.sleep(1.0)
        
        # Simulate loss decreasing and accuracy increasing
        current_loss = max(0.1, current_loss * 0.85 + random.uniform(-0.1, 0.1))
        current_acc = min(99.9, current_acc + 4.0 + random.uniform(-1, 2))
        
        print(f"[Epoch {epoch:02d}] Loss: {current_loss:.4f} | Acc: {current_acc:.2f}%")
        
        # 👉 TRACK METRICS IN JSON
        # These variables are tracked so they can be exported to a bulk CSV data report.
        pyruns.add_monitor(epoch=epoch, loss=current_loss, accuracy=current_acc)

    print("Finished training.")

if __name__ == "__main__":
    train()
