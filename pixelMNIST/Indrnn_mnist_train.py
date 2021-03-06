from __future__ import print_function
import sys
import argparse
import os
import time
import numpy as np
import copy
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F


# Set the random seed manually for reproducibility.
seed=100
torch.manual_seed(seed)
if torch.cuda.is_available():
  torch.cuda.manual_seed(seed)
else:
  print("WARNING: CUDA not available")

import opts     
parser = argparse.ArgumentParser(description='pytorch action')
opts.train_opts(parser)
args = parser.parse_args()
print(args)

import Indrnn_mnist_network


batch_size = args.batch_size
outputclass=10
indim=1
gradientclip_value=10
U_bound=Indrnn_mnist_network.U_bound
use_permute=args.use_permute
from Data_gen import DataHandler,evalDataHandler,testDataHandler
dh_train=DataHandler(batch_size)
dh_eval=evalDataHandler(batch_size)
dh_test=testDataHandler(batch_size)
num_train_batches=int(np.ceil(dh_train.GetDatasetSize()/(batch_size+0.0)))
num_eval_batches=int(np.ceil(dh_eval.GetDatasetSize()/(batch_size+0.0)))
num_test_batches=int(np.ceil(dh_test.GetDatasetSize()/(batch_size+0.0)))
x,y=dh_train.GetBatch()
seq_len=x.shape[1]
print(num_train_batches,num_test_batches)
feature_size=x.shape[2]
if seq_len!=args.seq_len:
  print('error seq_len')
  assert 2==3

model = Indrnn_mnist_network.stackedIndRNN_encoder(indim, outputclass)  
model.cuda()
criterion = nn.CrossEntropyLoss()

#Adam with lr 2e-4 works fine.
learning_rate=args.lr
if args.use_weightdecay_nohiddenW:
  param_decay=[]
  param_nodecay=[]
  for name, param in model.named_parameters():
    if 'weight_hh' in name or 'bias' in name:
      param_nodecay.append(param)      
      #print('parameters no weight decay: ',name)          
    else:
      param_decay.append(param)      
      #print('parameters with weight decay: ',name)          

  if args.opti=='sgd':
    optimizer = torch.optim.SGD([
            {'params': param_nodecay},
            {'params': param_decay, 'weight_decay': args.decayfactor}
        ], lr=learning_rate,momentum=0.9,nesterov=True)   
  else:                
    optimizer = torch.optim.Adam([
            {'params': param_nodecay},
            {'params': param_decay, 'weight_decay': args.decayfactor}
        ], lr=learning_rate) 
else:  
  if args.opti=='sgd':   
    optimizer=torch.optim.Adam(model.parameters(), lr=learning_rate,momentum=0.9,nesterov=True)
  else:                      
    optimizer=torch.optim.Adam(model.parameters(), lr=learning_rate)




def train(num_train_batches):
  model.train()
  tacc=0
  count=0
  start_time = time.time()
  for batchi in range(0,num_train_batches):
    inputs,targets=dh_train.GetBatch()
    inputs=inputs.transpose(1,0,2)
    
    inputs=torch.from_numpy(inputs).cuda()
    targets=torch.from_numpy(np.int64(targets)).cuda()

    model.zero_grad()
    if args.constrain_U:
      clip_weight(model,U_bound)
    output=model(inputs)
    loss = criterion(output, targets)

    pred = output.data.max(1)[1] # get the index of the max log-probability
    accuracy = pred.eq(targets.data).cpu().sum()      
          
    loss.backward()
    clip_gradient(model,gradientclip_value)
    optimizer.step()
    
    tacc=tacc+accuracy.numpy()/(0.0+targets.size(0))#loss.data.cpu().numpy()#accuracy
    count+=1
  elapsed = time.time() - start_time
  print ("training accuracy: ", tacc/(count+0.0)  )
  #print ('time per batch: ', elapsed/num_train_batches)
  
def set_bn_train(m):
    classname = m.__class__.__name__
    if classname.find('BatchNorm') != -1:
      m.train()       
def eval(dh,num_batches,Is_test=False,use_bn_trainstat=False):
  model.eval()
  if use_bn_trainstat:
    model.apply(set_bn_train)
  tacc=0
  count=0  
  start_time = time.time()
  while(1):  
    inputs,targets=dh.GetBatch()
    inputs=inputs.transpose(1,0,2)
    inputs=torch.from_numpy(inputs).cuda()
    targets=torch.from_numpy(np.int64(targets)).cuda()
        
    output=model(inputs)
    output=output.detach()
    pred = output.data.max(1)[1] # get the index of the max log-probability
    accuracy = pred.eq(targets.data).cpu().sum()        
    tacc+=accuracy.numpy()
    count+=1
    if count==num_batches:
      break
  elapsed = time.time() - start_time
  if Is_test:
    print ("test accuracy: ", tacc/(count*targets.data.size(0)+0.0)  )
  else:
    print ("eval accuracy: ", tacc/(count*targets.data.size(0)+0.0)  )
  #print ('eval time per batch: ', elapsed/(count+0.0))
  return tacc/(count*targets.data.size(0)+0.0)


def clip_gradient(model, clip):
    for p in model.parameters():
        p.grad.data.clamp_(-clip,clip)
        #print(p.size(),p.grad.data)

def adjust_learning_rate(optimizer, lr):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr     

def clip_weight(RNNmodel, clip):
    for name, param in RNNmodel.named_parameters():
      if 'weight_hh' in name:
        param.data.clamp_(-clip,clip)
    
lastacc=0
dispFreq=100
patience=0
reduced=1
for batchi in range(1,10000000):
  for i in range(num_train_batches//dispFreq):
    train(dispFreq)
  test_acc=eval(dh_eval,num_eval_batches)
  test_acc1=eval(dh_eval,num_eval_batches,False,True)#use the individual statistics of the batch

  if (test_acc >lastacc):
    model_clone = copy.deepcopy(model.state_dict())   
    opti_clone = copy.deepcopy(optimizer.state_dict()) 
    lastacc=test_acc
    patience=0
  elif patience>int(args.pThre/reduced+0.5):
    reduced=reduced*2
    print ('learning rate',learning_rate)
    print('epocs: ', batchi)
    model.load_state_dict(model_clone)
    optimizer.load_state_dict(opti_clone)
    patience=0
    learning_rate=learning_rate*0.1
    adjust_learning_rate(optimizer,learning_rate)       
    if learning_rate<args.end_rate:
      break  
    test_acc=eval(dh_test,num_test_batches,True)   
 
  else:
    patience+=1 
    
test_acc=eval(dh_test,num_test_batches,True)   
test_acc=eval(dh_test,num_test_batches,True,True)    
# save_name='indrnn_pixelmnist_model' 
# with open(save_name, 'wb') as f:
#     torch.save(model, f)



 
