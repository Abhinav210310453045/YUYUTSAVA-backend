import os
import shutil

path = '/Users/abhinav0087/Desktop/YUYUTSAVA-backend/workspace/test_cleanup'
if os.path.exists(path):
    shutil.rmtree(path)
    print(f'Removed {path}')
else:
    print(f'Path does not exist: {path}')