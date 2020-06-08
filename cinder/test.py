import os
import cinder
cinder_path = os.path.dirname(cinder.__file__)
patch_path =  os.path.abspath(os.getcwd())
print(cinder_path)