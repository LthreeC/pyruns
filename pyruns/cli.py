import os
import sys
import traceback

from .utils import get_logger

logger = get_logger(__name__)


def pyr():
    """pyr <script.py> - 快速启动配置 UI"""
    
    if len(sys.argv) < 2:
        print(f"Usage: {os.path.basename(sys.argv[0])} <script.py>")
        sys.exit(1)

    # 1. 确定路径
    filepath = os.path.abspath(sys.argv[1])
    if not os.path.exists(filepath):
        print(f"Error: '{sys.argv[1]}' not found.")
        sys.exit(1)

    try:
        from pyruns.utils.parse_utils import (
            detect_config_source_fast,
            extract_argparse_params,
            generate_config_file,
            resolve_config_path,
        )
        
        # 2. 快速检测模式 (< 1ms)
        mode, extra = detect_config_source_fast(filepath)
        
        file_dir = os.path.dirname(filepath)
        pyruns_dir = os.path.join(file_dir, "_pyruns_")  # 固定路径
        config_file = None
        
        if mode == "argparse":
            # AST 解析只在这里执行
            params = extract_argparse_params(filepath)
            generate_config_file(filepath, params)  # 生成到 pyruns_dir
            config_file = os.path.join(pyruns_dir, "config_default.yaml")
            logger.info(f"[pyruns] argparse: {len(params)} params")
            
        elif mode == "pyruns_read":
            if extra:  # 如果指定了配置文件路径
                config_file = resolve_config_path(extra, file_dir)
                if not config_file:
                    logger.error(f"Error: Config '{extra}' not found.")
                    sys.exit(1)
                logger.info(f"[pyruns] config: {extra}")
            else:
                logger.info("[pyruns] pyruns.read() called with no config path")
            
        else:
            logger.info(f"[pyruns] mode: {mode}")

        # 确保目录存在
        if not os.path.exists(pyruns_dir):
            os.makedirs(pyruns_dir, exist_ok=True)
            logger.info(f"[pyruns] Created {pyruns_dir}")

        # 3. 设置环境变量
        os.environ["PYRUNS_ROOT"] = pyruns_dir
        os.environ["PYRUNS_SCRIPT"] = filepath
        
        logger.info(f"[pyruns] ROOT: {pyruns_dir}")

        # 4. 启动 UI
        sys.argv = [sys.argv[0]]
        from pyruns.ui.app import main
        main()

    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    pyr()