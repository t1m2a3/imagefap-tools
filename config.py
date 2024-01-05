'''
Configuration placeholder and loader.
'''

def _load_config():
    import os
    import sys
    import yaml

    base_dir = os.path.abspath(os.path.dirname(__file__) or '.')
    config_filename = os.path.join(base_dir, 'config.yaml')
    with open(config_filename) as f:
        config_dict = yaml.safe_load(f.read())

    # set module attributes
    config = sys.modules[__name__]
    for k, v in config_dict.items():
        setattr(config, k, v)

_load_config()
