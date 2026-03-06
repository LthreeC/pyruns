import hydra
from omegaconf import DictConfig, OmegaConf

@hydra.main(version_base=None, config_path="conf", config_name="config")
def train(cfg: DictConfig):
    # 打印完整配置（自动合并所有yaml）
    print(OmegaConf.to_yaml(cfg))
    
    # 访问配置
    print(f"Model: {cfg.model.name}")
    print(f"Optimizer: {cfg.optimizer.name}")
    print(f"Learning rate: {cfg.train.lr}")
    print(f"Batch size: {cfg.train.batch_size}")
    
    # 实际训练代码
    # model = build_model(cfg.model)
    # optimizer = build_optimizer(cfg.optimizer, model)
    # ...

if __name__ == "__main__":
    train()
    
    
# 使用 ViT 模型（自动加载 conf/model/vit.yaml）
# python train.py model=vit

# # 使用 SGD 优化器（自动加载 conf/optimizer/sgd.yaml）
# python train.py optimizer=sgd

# # 同时切换多个
# python train.py model=vit optimizer=sgd dataset=imagenet

# # 修改学习率
# python train.py train.lr=0.01

# # 修改batch size
# python train.py train.batch_size=64

# # 修改模型的dropout
# python train.py model.dropout=0.2

# # 组合使用
# python train.py model=vit train.lr=0.005 train.batch_size=128


# # 网格搜索：3个学习率 × 2个batch size = 6次运行
# python train.py -m train.lr=0.001,0.01,0.1 train.batch_size=32,64

# # 自动运行6次实验，每次保存独立的输出目录