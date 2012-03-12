'''
Example configuration file
'''

# Audio config
class AudioConfig:
    samplerate = 44100
    windowsize = 2048
    stepsize   = 1024

# User Config
source = 'John'

# Extractor Chain
from analyze.feature import FFT, Loudness

# FrameModel
# TODO: FrameModel needs to have a metaclass that knows how to map extractors
# to properties, and map those properties to some database
from model.frame import Frames, Feature

class FrameModel(Frames):    
    fft      = Feature(FFT,      store = True, needs = None)
    loudness = Feature(Loudness, store = True, needs = fft)
    

# Data backends
from model.pattern import Pattern
from model.pipeline import Pipeline
from data.pattern import InMemory
from data.learn import LearningController
from data.frame import PyTablesFrameController
data = {
        
    Pattern    : InMemory(),
    Pipeline   : LearningController()
}


from environment import Environment
Z = Environment(
                source,                             # name of this application
                FrameModel,                         # our frame model
                PyTablesFrameController,            # FrameController class
                (FrameModel,'datastore/frames.h5'), # FrameController args
                data,                               # data-backend config
                AudioConfig)                        # audio configuration

