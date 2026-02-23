import time
import os

try:
    import pyruns
except ImportError:
    print("❌ 请先安装 pyruns: `pip install pyruns`")
    exit(1)

def main():
    # Pyruns 会自动加载目录下的 config.yaml，并支持对象式（.）访问
    config = pyruns.load()
    
    # --- 1. 配置参数展示 (三级结构) ---
    # 第一级：project, model, training
    # 第二级：training.hyperparams, training.resources
    # 第三级：training.resources.gpu_config
    
    p_name = config.project.name
    version = config.project.version
    
    m_type = config.model.type
    layers = config.model.layers
    
    tp = config.training.hyperparams
    res = config.training.resources
    
    print(f"--- 🛠  项目: {p_name} (v{version}) ---")
    print(f"--- 🤖 模型: {m_type} | 层数: {layers} ---")
    print(f"--- 🚀 资源: {res.device} | 显存优化: {res.gpu_config.memory_frac} ---")
    print("-" * 40)

    # --- 2. 模拟训练逻辑 ---
    print(f"开始使用 {tp.optimizer} 优化器训练，学习率: {tp.lr}...")
    
    total_epochs = tp.epochs
    for epoch in range(1, total_epochs + 1):
        # 模拟计算，总时长严格控制在几秒内
        time.sleep(0.3) 
        
        # 模拟一个随随机扰动的 Loss
        loss = (1.0 / (epoch * tp.lr * 10)) + (time.time() % 0.1)
        
        print(f"[Epoch {epoch:02d}/{total_epochs}] "
              f"Loss: {loss:.4f} | "
              f"Dropout: {config.model.dropout} | "
              f"Precision: {res.precision}")

    print("-" * 40)
    print(f"✅ 任务完成！结果已保存至: {config.project.output_dir}")

if __name__ == "__main__":
    main()