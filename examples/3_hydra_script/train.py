import hydra
from omegaconf import DictConfig, OmegaConf
import time
import random

@hydra.main(version_base=None, config_path="conf", config_name="config")
def train(cfg: DictConfig) -> None:
    """
    模拟训练流程 - 纯打印，无实际计算
    """
    
    # ========== 1. 打印完整配置 ==========
    print("=" * 80)
    print("🔧 CONFIGURATION")
    print("=" * 80)
    print(OmegaConf.to_yaml(cfg))
    print("=" * 80)
    
    # ========== 2. 设置随机种子 ==========
    random.seed(cfg.experiment.seed)
    print(f"\n🎲 Random seed set to: {cfg.experiment.seed}")
    
    # ========== 3. 打印模型信息 ==========
    print(f"\n📦 Model: {cfg.model.name}")
    print(f"   - Architecture: {cfg.model.architecture.hidden_dims}")
    print(f"   - Total parameters: {cfg.model.params.total_params:,}")
    print(f"   - Dropout: {cfg.model.architecture.dropout}")
    
    # ========== 4. 打印优化器信息 ==========
    print(f"\n⚙️  Optimizer: {cfg.optimizer.name}")
    print(f"   - Learning rate: {cfg.optimizer.lr}")
    if cfg.optimizer.name == "Adam":
        print(f"   - Betas: {cfg.optimizer.betas}")
        print(f"   - Weight decay: {cfg.optimizer.weight_decay}")
    else:
        print(f"   - Momentum: {cfg.optimizer.momentum}")
        print(f"   - Nesterov: {cfg.optimizer.nesterov}")
    
    # ========== 5. 打印数据集信息 ==========
    print(f"\n📊 Dataset: {cfg.data.name}")
    print(f"   - Samples: {cfg.data.num_samples}")
    print(f"   - Features: {cfg.data.num_features}")
    print(f"   - Classes: {cfg.data.num_classes}")
    print(f"   - Train split: {cfg.data.train_split * 100}%")
    
    train_samples = int(cfg.data.num_samples * cfg.data.train_split)
    val_samples = cfg.data.num_samples - train_samples
    print(f"   - Train size: {train_samples}")
    print(f"   - Val size: {val_samples}")
    
    # ========== 6. 模拟训练循环 ==========
    print(f"\n🚀 Starting training for {cfg.train.epochs} epochs...")
    print(f"   - Batch size: {cfg.train.batch_size}")
    print(f"   - Device: {cfg.experiment.device}")
    print("-" * 80)
    
    for epoch in range(1, cfg.train.epochs + 1):
        # 模拟训练时间
        time.sleep(0.3)
        
        # 生成假的训练指标
        train_loss = 2.5 / (epoch * 0.5 + 1) + random.uniform(-0.1, 0.1)
        train_acc = min(95, epoch * 8 + random.uniform(-2, 2))
        val_loss = train_loss + random.uniform(0, 0.3)
        val_acc = train_acc - random.uniform(0, 5)
        
        if epoch % cfg.train.print_every == 0 or epoch == cfg.train.epochs:
            print(f"Epoch [{epoch:2d}/{cfg.train.epochs}] "
                  f"| Train Loss: {train_loss:.4f} "
                  f"| Train Acc: {train_acc:.2f}% "
                  f"| Val Loss: {val_loss:.4f} "
                  f"| Val Acc: {val_acc:.2f}%")
    
    print("-" * 80)
    
    # ========== 7. 保存信息 ==========
    print(f"\n💾 Model would be saved to: {cfg.experiment.save_dir}")
    print(f"   - Experiment name: {cfg.experiment.name}")
    
    # ========== 8. 最终总结 ==========
    print("\n" + "=" * 80)
    print("✅ Training completed!")
    print(f"   - Final train accuracy: {train_acc:.2f}%")
    print(f"   - Final val accuracy: {val_acc:.2f}%")
    print(f"   - Total time: ~{cfg.train.epochs * 0.3:.1f}s (simulated)")
    print("=" * 80)
    
    
    for i in range(1000):
        print(i)


if __name__ == "__main__":
    train()