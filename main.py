import sys
import yaml
import os
from blogs.cnblogs import sync_to_cnblogs

def load_config():
    """加载配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"错误：配置文件 {config_path} 不存在")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"错误：配置文件格式不正确 - {str(e)}")
        sys.exit(1)

def main():
    if len(sys.argv) != 2:
        print("使用方法：python main.py <markdown文件路径>")
        sys.exit(1)

    # 加载配置
    config = load_config()
    
    # 同步到博客园
    if config['main']['cnblogs']:
        sync_to_cnblogs(sys.argv[1], config['cnblogs'])

if __name__ == '__main__':
    main()
