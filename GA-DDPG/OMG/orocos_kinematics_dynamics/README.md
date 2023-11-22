 
## Installation
### SIP  

```bash  

cd sip-4.19.3
python configure.py
make -j8; sudo make install
```

### KDL C++ 

```bash 
export ROS_PYTHON_VERSION=3
cd ../orocos_kdl
mkdir build; cd build;
cmake ..
make -j8; sudo make install
```

### Python Binding  

```bash 
cd ../../python_orocos_kdl
mkdir build; cd build;
cmake ..  -DPYTHON_VERSION=3.6.9 -DPYTHON_EXECUTABLE=~/anaconda2/envs/omg/bin/python3.6
make -j8;  cp PyKDL.so ~/anaconda2/envs/omg/lib/python3.6/site-packages/
```

## Note
1. Sanity check ``` python -c "import PyKDL" ```
2. Python 2 users should replace all appearances of python3 accordingly.
3. The anaconda path should also be modified accordingly
