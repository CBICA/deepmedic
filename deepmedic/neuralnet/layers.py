# Copyright (c) 2016, Konstantinos Kamnitsas
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the BSD license. See the accompanying LICENSE file
# or read the terms at https://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, print_function, division
from six.moves import xrange
import numpy as np
import random

import theano
import theano.tensor as T

try:
    from sys import maxint as MAX_INT
except ImportError:
    # python3 compatibility
    from sys import maxsize as MAX_INT

from deepmedic.neuralnet.ops import applyDropout, makeBiasParamsAndApplyToFms, applyRelu, applyPrelu, applyElu, applySelu, pool3dMirrorPad
from deepmedic.neuralnet.ops import applyBn, createAndInitializeWeightsTensor, convolveWithGivenWeightMatrix, applySoftmaxToFmAndReturnProbYandPredY


####################
# Helper functions #
####################

def checkDimsOfYpredAndYEqual(y, yPred, stringTrainOrVal) :
    if y.ndim != yPred.ndim:
        raise TypeError( "ERROR! y did not have the same shape as y_pred during " + stringTrainOrVal,
                        ('y', y.type, 'y_pred', yPred.type) )
        

#################################################################
#                         Layer Types                           #
#################################################################
# Inheritance:
# Block -> ConvLayer -> LowRankConvLayer
#                L-----> ConvLayerWithSoftmax

class Block(object):
    
    def __init__(self) :
        # === Input to the layer ===
        self.inputTrain = None
        self.inputVal = None
        self.inputTest = None
        self.inputShapeTrain = None
        self.inputShapeVal = None
        self.inputShapeTest = None
        
        # === Basic architecture parameters === 
        self._numberOfFeatureMaps = None
        self._poolingParameters = None
        
        #=== All Trainable Parameters of the Block ===
        self._appliedBnInLayer = None # This flag is a combination of rollingAverageForBn>0 AND useBnFlag, with the latter used for the 1st layers of pathways (on image).
        
        # All trainable parameters
        # NOTE: VIOLATED _HIDDEN ENCAPSULATION BY THE FUNCTION THAT TRANSFERS PRETRAINED WEIGHTS deepmed.neuralnet.transferParameters.transferParametersBetweenLayers.
        # TEMPORARY TILL THE API GETS FIXED (AFTER DA)!
        self.params = [] # W, (gbn), b, (aPrelu)
        self._W = None # Careful. LowRank does not set this. Uses ._WperSubconv
        self._b = None # shape: a vector with one value per FM of the input
        self._gBn = None # ONLY WHEN BN is applied
        self._aPrelu = None # ONLY WHEN PreLu
        
        # ONLY WHEN BN! All of these are for the rolling average! If I fix this, only 2 will remain!
        self._muBnsArrayForRollingAverage = None # Array
        self._varBnsArrayForRollingAverage = None # Arrays
        self._rollingAverageForBatchNormalizationOverThatManyBatches = None
        self._indexWhereRollingAverageIs = 0 #Index in the rolling-average matrices of the layers, of the entry to update in the next batch.
        self._sharedNewMu_B = None # last value shared, to update the rolling average array.
        self._sharedNewVar_B = None
        self._newMu_B = None # last value tensor, to update the corresponding shared.
        self._newVar_B = None
        
        
        # === Output of the block ===
        self.outputTrain = None
        self.outputVal = None
        self.outputTest = None
        self.outputShapeTrain = None
        self.outputShapeVal = None
        self.outputShapeTest = None
        # New and probably temporary, for the residual connections to be "visible".
        self.outputAfterResidualConnIfAnyAtOutpTrain = None
        self.outputAfterResidualConnIfAnyAtOutpVal = None
        self.outputAfterResidualConnIfAnyAtOutpTest = None
        
        # ==== Target Block Connected to that layer (softmax, regression, auxiliary loss etc), if any ======
        self.targetBlock = None
        
    # Setters
    def _setBlocksInputAttributes(self, inputToLayerTrain, inputToLayerVal, inputToLayerTest, inputToLayerShapeTrain, inputToLayerShapeVal, inputToLayerShapeTest) :
        self.inputTrain = inputToLayerTrain
        self.inputVal = inputToLayerVal
        self.inputTest = inputToLayerTest
        self.inputShapeTrain = inputToLayerShapeTrain
        self.inputShapeVal = inputToLayerShapeVal
        self.inputShapeTest = inputToLayerShapeTest
        
    def _setBlocksArchitectureAttributes(self, filterShape, poolingParameters) :
        self._numberOfFeatureMaps = filterShape[0] # Of the output! Used in trainValidationVisualise.py. Not of the input!
        assert self.inputShapeTrain[1] == filterShape[1]
        self._poolingParameters = poolingParameters
        
    def _setBlocksOutputAttributes(self, outputTrain, outputVal, outputTest, outputShapeTrain, outputShapeVal, outputShapeTest) :
        self.outputTrain = outputTrain
        self.outputVal = outputVal
        self.outputTest = outputTest
        self.outputShapeTrain = outputShapeTrain
        self.outputShapeVal = outputShapeVal
        self.outputShapeTest = outputShapeTest
        # New and probably temporary, for the residual connections to be "visible".
        self.outputAfterResidualConnIfAnyAtOutpTrain = self.outputTrain
        self.outputAfterResidualConnIfAnyAtOutpVal = self.outputVal
        self.outputAfterResidualConnIfAnyAtOutpTest = self.outputTest
        
    def setTargetBlock(self, targetBlockInstance):
        # targetBlockInstance : eg softmax layer. Future: Regression layer, or other auxiliary classifiers.
        self.targetBlock = targetBlockInstance
    # Getters
    def getNumberOfFeatureMaps(self):
        return self._numberOfFeatureMaps
    def fmsActivations(self, indices_of_fms_in_layer_to_visualise_from_to_exclusive) :
        return self.outputTest[:, indices_of_fms_in_layer_to_visualise_from_to_exclusive[0] : indices_of_fms_in_layer_to_visualise_from_to_exclusive[1], :, :, :]
    
    # Other API
    def getL1RegCost(self) : #Called for L1 weigths regularisation
        raise NotImplementedMethod() # Abstract implementation. Children classes should implement this.
    def getL2RegCost(self) : #Called for L2 weigths regularisation
        raise NotImplementedMethod()
    def getTrainableParams(self):
        if self.targetBlock == None :
            return self.params
        else :
            return self.params + self.targetBlock.getTrainableParams()
        
    def updateTheMatricesWithTheLastMusAndVarsForTheRollingAverageOfBNInference(self):
        # This function should be erazed when I reimplement the Rolling average.
        if self._appliedBnInLayer :
            muArrayValue = self._muBnsArrayForRollingAverage.get_value()
            muArrayValue[self._indexWhereRollingAverageIs] = self._sharedNewMu_B.get_value()
            self._muBnsArrayForRollingAverage.set_value(muArrayValue, borrow=True)
            
            varArrayValue = self._varBnsArrayForRollingAverage.get_value()
            varArrayValue[self._indexWhereRollingAverageIs] = self._sharedNewVar_B.get_value()
            self._varBnsArrayForRollingAverage.set_value(varArrayValue, borrow=True)
            self._indexWhereRollingAverageIs = (self._indexWhereRollingAverageIs + 1) % self._rollingAverageForBatchNormalizationOverThatManyBatches
            
    def getUpdatesForBnRollingAverage(self) :
        # This function or something similar should stay, even if I clean the BN rolling average.
        if self._appliedBnInLayer :
            #CAREFUL: WARN, PROBLEM, THEANO BUG! If a layer has only 1FM, the .newMu_B ends up being of type (true,) instead of vector!!! Error!!!
            return [(self._sharedNewMu_B, self._newMu_B),
                    (self._sharedNewVar_B, self._newVar_B) ]
        else :
            return []
        
class ConvLayer(Block):
    
    def __init__(self) :
        Block.__init__(self)
        self._activationFunctionType = "" #linear, relu or prelu
        
    def _processInputWithBnNonLinearityDropoutPooling(self,
                rng,
                inputToLayerTrain,
                inputToLayerVal,
                inputToLayerTest,
                inputToLayerShapeTrain,
                inputToLayerShapeVal,
                inputToLayerShapeTest,
                useBnFlag, # Must be true to do BN. Used to not allow doing BN on first layers straight on image, even if rollingAvForBnOverThayManyBatches > 0.
                rollingAverageForBatchNormalizationOverThatManyBatches, #If this is <= 0, we are not using BatchNormalization, even if above is True.
                activationFunc,
                dropoutRate) :
        # ---------------- Order of what is applied -----------------
        #  Input -> [ BatchNorm OR biases applied] -> NonLinearity -> DropOut -> Pooling --> Conv ] # ala He et al "Identity Mappings in Deep Residual Networks" 2016
        # -----------------------------------------------------------
        
        #---------------------------------------------------------
        #------------------ Batch Normalization ------------------
        #---------------------------------------------------------
        if useBnFlag and rollingAverageForBatchNormalizationOverThatManyBatches > 0 :
            self._appliedBnInLayer = True
            self._rollingAverageForBatchNormalizationOverThatManyBatches = rollingAverageForBatchNormalizationOverThatManyBatches
            (inputToNonLinearityTrain,
            inputToNonLinearityVal,
            inputToNonLinearityTest,
            self._gBn,
            self._b,
            # For rolling average :
            self._muBnsArrayForRollingAverage,
            self._varBnsArrayForRollingAverage,
            self._sharedNewMu_B,
            self._sharedNewVar_B,
            self._newMu_B,
            self._newVar_B
            ) = applyBn( rollingAverageForBatchNormalizationOverThatManyBatches, inputToLayerTrain, inputToLayerVal, inputToLayerTest, inputToLayerShapeTrain)
            self.params = self.params + [self._gBn, self._b]
        else : #Not using batch normalization
            self._appliedBnInLayer = False
            #make the bias terms and apply them. Like the old days before BN's own learnt bias terms.
            numberOfInputChannels = inputToLayerShapeTrain[1]
            
            (self._b,
            inputToNonLinearityTrain,
            inputToNonLinearityVal,
            inputToNonLinearityTest) = makeBiasParamsAndApplyToFms( inputToLayerTrain, inputToLayerVal, inputToLayerTest, numberOfInputChannels )
            self.params = self.params + [self._b]
            
        #--------------------------------------------------------
        #------------ Apply Activation/ non-linearity -----------
        #--------------------------------------------------------
        self._activationFunctionType = activationFunc
        if self._activationFunctionType == "linear" : # -1 stands for "no nonlinearity". Used for input layers of the pathway.
            ( inputToDropoutTrain, inputToDropoutVal, inputToDropoutTest ) = (inputToNonLinearityTrain, inputToNonLinearityVal, inputToNonLinearityTest)
        elif self._activationFunctionType == "relu" :
            ( inputToDropoutTrain, inputToDropoutVal, inputToDropoutTest ) = applyRelu(inputToNonLinearityTrain, inputToNonLinearityVal, inputToNonLinearityTest)
        elif self._activationFunctionType == "prelu" :
            numberOfInputChannels = inputToLayerShapeTrain[1]
            ( self._aPrelu, inputToDropoutTrain, inputToDropoutVal, inputToDropoutTest ) = applyPrelu(inputToNonLinearityTrain, inputToNonLinearityVal, inputToNonLinearityTest, numberOfInputChannels)
            self.params = self.params + [self._aPrelu]
        elif self._activationFunctionType == "elu" :
            ( inputToDropoutTrain, inputToDropoutVal, inputToDropoutTest ) = applyElu(inputToNonLinearityTrain, inputToNonLinearityVal, inputToNonLinearityTest)
        elif self._activationFunctionType == "selu" :
            ( inputToDropoutTrain, inputToDropoutVal, inputToDropoutTest ) = applySelu(inputToNonLinearityTrain, inputToNonLinearityVal, inputToNonLinearityTest)
            
        #------------------------------------
        #------------- Dropout --------------
        #------------------------------------
        (inputToPoolTrain, inputToPoolVal, inputToPoolTest) = applyDropout(rng, dropoutRate, inputToLayerShapeTrain, inputToDropoutTrain, inputToDropoutVal, inputToDropoutTest)
        
        #-------------------------------------------------------
        #-----------  Pooling ----------------------------------
        #-------------------------------------------------------
        if self._poolingParameters == [] : #no max pooling before this conv
            inputToConvTrain = inputToPoolTrain
            inputToConvVal = inputToPoolVal
            inputToConvTest = inputToPoolTest
            
            inputToConvShapeTrain = inputToLayerShapeTrain
            inputToConvShapeVal = inputToLayerShapeVal
            inputToConvShapeTest = inputToLayerShapeTest
        else : #Max pooling is actually happening here...
            (inputToConvTrain, inputToConvShapeTrain) = pool3dMirrorPad(inputToPoolTrain, inputToLayerShapeTrain, self._poolingParameters)
            (inputToConvVal, inputToConvShapeVal) = pool3dMirrorPad(inputToPoolVal, inputToLayerShapeVal, self._poolingParameters)
            (inputToConvTest, inputToConvShapeTest) = pool3dMirrorPad(inputToPoolTest, inputToLayerShapeTest, self._poolingParameters)
            
        return (inputToConvTrain, inputToConvVal, inputToConvTest,
                inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest )
        
    def _createWeightsTensorAndConvolve(self, rng, filterShape, convWInitMethod, 
                                        inputToConvTrain, inputToConvVal, inputToConvTest,
                                        inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest) :
        #-----------------------------------------------
        #------------------ Convolution ----------------
        #-----------------------------------------------
        #----- Initialise the weights -----
        # W shape: [#FMs of this layer, #FMs of Input, rKernDim, cKernDim, zKernDim]
        self._W = createAndInitializeWeightsTensor(filterShape, convWInitMethod, rng)
        self.params = [self._W] + self.params
        
        #---------- Convolve --------------
        tupleWithOuputAndShapeTrValTest = convolveWithGivenWeightMatrix(self._W, filterShape, inputToConvTrain, inputToConvVal, inputToConvTest, inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest)
        
        return tupleWithOuputAndShapeTrValTest
    
    # The main function that builds this.
    def makeLayer(self,
                rng,
                inputToLayerTrain,
                inputToLayerVal,
                inputToLayerTest,
                inputToLayerShapeTrain,
                inputToLayerShapeVal,
                inputToLayerShapeTest,
                filterShape,
                poolingParameters, # Can be []
                convWInitMethod,
                useBnFlag, # Must be true to do BN. Used to not allow doing BN on first layers straight on image, even if rollingAvForBnOverThayManyBatches > 0.
                rollingAverageForBatchNormalizationOverThatManyBatches, #If this is <= 0, we are not using BatchNormalization, even if above is True.
                activationFunc="relu",
                dropoutRate=0.0):
        """
        type rng: numpy.random.RandomState
        param rng: a random number generator used to initialize weights
        
        type inputToLayer:  tensor5 = theano.tensor.TensorType(dtype='float32', broadcastable=(False, False, False, False, False))
        param inputToLayer: symbolic image tensor, of shape inputToLayerShape
        
        type filterShape: tuple or list of length 5
        param filterShape: (number of filters, num input feature maps,
                            filter height, filter width, filter depth)
                            
        type inputToLayerShape: tuple or list of length 5
        param inputToLayerShape: (batch size, num input feature maps,
                            image height, image width, filter depth)
        """
        self._setBlocksInputAttributes(inputToLayerTrain, inputToLayerVal, inputToLayerTest, inputToLayerShapeTrain, inputToLayerShapeVal, inputToLayerShapeTest)
        self._setBlocksArchitectureAttributes(filterShape, poolingParameters)
        
        # Apply all the straightforward operations on the input, such as BN, activation function, dropout, pooling        
        (inputToConvTrain, inputToConvVal, inputToConvTest,
        inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest) = self._processInputWithBnNonLinearityDropoutPooling( rng,
                                                                                        inputToLayerTrain,
                                                                                        inputToLayerVal,
                                                                                        inputToLayerTest,
                                                                                        inputToLayerShapeTrain,
                                                                                        inputToLayerShapeVal,
                                                                                        inputToLayerShapeTest,
                                                                                        useBnFlag,
                                                                                        rollingAverageForBatchNormalizationOverThatManyBatches,
                                                                                        activationFunc,
                                                                                        dropoutRate)
        
        tupleWithOuputAndShapeTrValTest = self._createWeightsTensorAndConvolve( rng, filterShape, convWInitMethod, 
                                                                                inputToConvTrain, inputToConvVal, inputToConvTest,
                                                                                inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest)
        
        self._setBlocksOutputAttributes(*tupleWithOuputAndShapeTrValTest)
        
    # Override parent's abstract classes.
    def getL1RegCost(self) : #Called for L1 weigths regularisation
        return abs(self._W).sum()
    def getL2RegCost(self) : #Called for L2 weigths regularisation
        return (self._W ** 2).sum()
    
    
# Ala Yani Ioannou et al, Training CNNs with Low-Rank Filters For Efficient Image Classification, ICLR 2016. Allowed Ranks: Rank=1 or 2.
class LowRankConvLayer(ConvLayer):
    def __init__(self, rank=2) :
        ConvLayer.__init__(self)
        
        self._WperSubconv = None # List of ._W theano tensors. One per low-rank subconv. Treat carefully. 
        del(self._W) # The ._W of the Block parent is not used.
        self._rank = rank # 1 or 2 dimensions
        
    def _cropSubconvOutputsToSameDimsAndConcatenateFms( self,
                                                        rSubconvOutput, rSubconvOutputShape,
                                                        cSubconvOutput, cSubconvOutputShape,
                                                        zSubconvOutput, zSubconvOutputShape,
                                                        filterShape) :
        assert (rSubconvOutputShape[0] == cSubconvOutputShape[0]) and (cSubconvOutputShape[0] == zSubconvOutputShape[0]) # same batch size.
        
        concatOutputShape = [ rSubconvOutputShape[0],
                                rSubconvOutputShape[1] + cSubconvOutputShape[1] + zSubconvOutputShape[1],
                                rSubconvOutputShape[2],
                                cSubconvOutputShape[3],
                                zSubconvOutputShape[4]
                                ]
        rCropSlice = slice( (filterShape[2]-1)//2, (filterShape[2]-1)//2 + concatOutputShape[2] )
        cCropSlice = slice( (filterShape[3]-1)//2, (filterShape[3]-1)//2 + concatOutputShape[3] )
        zCropSlice = slice( (filterShape[4]-1)//2, (filterShape[4]-1)//2 + concatOutputShape[4] )
        rSubconvOutputCropped = rSubconvOutput[:,:, :, cCropSlice if self._rank == 1 else slice(0, MAX_INT), zCropSlice  ]
        cSubconvOutputCropped = cSubconvOutput[:,:, rCropSlice, :, zCropSlice if self._rank == 1 else slice(0, MAX_INT) ]
        zSubconvOutputCropped = zSubconvOutput[:,:, rCropSlice if self._rank == 1 else slice(0, MAX_INT), cCropSlice, : ]
        concatSubconvOutputs = T.concatenate([rSubconvOutputCropped, cSubconvOutputCropped, zSubconvOutputCropped], axis=1) #concatenate the FMs
        
        return (concatSubconvOutputs, concatOutputShape)
    
    # Overload the ConvLayer's function. Called from makeLayer. The only different behaviour, because BN, ActivationFunc, DropOut and Pooling are done on a per-FM fashion.        
    def _createWeightsTensorAndConvolve(self, rng, filterShape, convWInitMethod, 
                                        inputToConvTrain, inputToConvVal, inputToConvTest,
                                        inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest) :
        # Behaviour: Create W, set self._W, set self.params, convolve, return ouput and outputShape.
        # The created filters are either 1-dimensional (rank=1) or 2-dim (rank=2), depending  on the self._rank
        # If 1-dim: rSubconv is the input convolved with the row-1dimensional filter.
        # If 2-dim: rSubconv is the input convolved with the RC-2D filter, cSubconv with CZ-2D filter, zSubconv with ZR-2D filter. 
        
        #----- Initialise the weights and Convolve for 3 separate, low rank filters, R,C,Z. -----
        # W shape: [#FMs of this layer, #FMs of Input, rKernDim, cKernDim, zKernDim]
        
        rSubconvFilterShape = [ filterShape[0]//3, filterShape[1], filterShape[2], 1 if self._rank == 1 else filterShape[3], 1 ]
        rSubconvW = createAndInitializeWeightsTensor(rSubconvFilterShape, convWInitMethod, rng)
        rSubconvTupleWithOuputAndShapeTrValTest = convolveWithGivenWeightMatrix(rSubconvW, rSubconvFilterShape, inputToConvTrain, inputToConvVal, inputToConvTest, inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest)
        
        cSubconvFilterShape = [ filterShape[0]//3, filterShape[1], 1, filterShape[3], 1 if self._rank == 1 else filterShape[4] ]
        cSubconvW = createAndInitializeWeightsTensor(cSubconvFilterShape, convWInitMethod, rng)
        cSubconvTupleWithOuputAndShapeTrValTest = convolveWithGivenWeightMatrix(cSubconvW, cSubconvFilterShape, inputToConvTrain, inputToConvVal, inputToConvTest, inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest)
        
        numberOfFmsForTotalToBeExact = filterShape[0] - 2*(filterShape[0]//3) # Cause of possibly inexact integer division.
        zSubconvFilterShape = [ numberOfFmsForTotalToBeExact, filterShape[1], 1 if self._rank == 1 else filterShape[2], 1, filterShape[4] ]
        zSubconvW = createAndInitializeWeightsTensor(zSubconvFilterShape, convWInitMethod, rng)
        zSubconvTupleWithOuputAndShapeTrValTest = convolveWithGivenWeightMatrix(zSubconvW, zSubconvFilterShape, inputToConvTrain, inputToConvVal, inputToConvTest, inputToConvShapeTrain, inputToConvShapeVal, inputToConvShapeTest)
        
        # Set the W attribute and trainable parameters.
        self._WperSubconv = [rSubconvW, cSubconvW, zSubconvW] # Bear in mind that these sub tensors have different shapes! Treat carefully.
        self.params = self._WperSubconv + self.params
        
        # concatenate together.
        (concatSubconvOutputsTrain, concatOutputShapeTrain) = self._cropSubconvOutputsToSameDimsAndConcatenateFms(rSubconvTupleWithOuputAndShapeTrValTest[0], rSubconvTupleWithOuputAndShapeTrValTest[3],
                                                                                                        cSubconvTupleWithOuputAndShapeTrValTest[0], cSubconvTupleWithOuputAndShapeTrValTest[3],
                                                                                                        zSubconvTupleWithOuputAndShapeTrValTest[0], zSubconvTupleWithOuputAndShapeTrValTest[3],
                                                                                                        filterShape)
        (concatSubconvOutputsVal, concatOutputShapeVal) = self._cropSubconvOutputsToSameDimsAndConcatenateFms(rSubconvTupleWithOuputAndShapeTrValTest[1], rSubconvTupleWithOuputAndShapeTrValTest[4],
                                                                                                        cSubconvTupleWithOuputAndShapeTrValTest[1], cSubconvTupleWithOuputAndShapeTrValTest[4],
                                                                                                        zSubconvTupleWithOuputAndShapeTrValTest[1], zSubconvTupleWithOuputAndShapeTrValTest[4],
                                                                                                        filterShape)
        (concatSubconvOutputsTest, concatOutputShapeTest) = self._cropSubconvOutputsToSameDimsAndConcatenateFms(rSubconvTupleWithOuputAndShapeTrValTest[2], rSubconvTupleWithOuputAndShapeTrValTest[5],
                                                                                                        cSubconvTupleWithOuputAndShapeTrValTest[2], cSubconvTupleWithOuputAndShapeTrValTest[5],
                                                                                                        zSubconvTupleWithOuputAndShapeTrValTest[2], zSubconvTupleWithOuputAndShapeTrValTest[5],
                                                                                                        filterShape)
        
        return (concatSubconvOutputsTrain, concatSubconvOutputsVal, concatSubconvOutputsTest, concatOutputShapeTrain, concatOutputShapeVal, concatOutputShapeTest)
        
        
    # Implement parent's abstract classes.
    def getL1RegCost(self) : #Called for L1 weigths regularisation
        l1Cost = 0
        for wOfSubconv in self._WperSubconv : l1Cost += abs(wOfSubconv).sum()
        return l1Cost
    def getL2RegCost(self) : #Called for L2 weigths regularisation
        l2Cost = 0
        for wOfSubconv in self._WperSubconv : l2Cost += (wOfSubconv ** 2).sum()
        return l2Cost
    def getW(self):
        print("ERROR: For LowRankConvLayer, the ._W is not used! Use ._WperSubconv instead and treat carefully!! Exiting!"); exit(1)
        
        
class SoftmaxLayer(Block):
    """ Softmax for classification. Note, this is simply the softmax function, after adding bias. Not a ConvLayer """
    
    def __init__(self):
        Block.__init__(self)
        self._numberOfOutputClasses = None
        #self._b = None # The only type of trainable parameter that a softmax layer has.
        self._softmaxTemperature = None
        
    def makeLayer(  self,
                    rng,
                    layerConnected, # the basic layer, at the output of which to connect this softmax.
                    softmaxTemperature = 1):
        
        self._numberOfOutputClasses = layerConnected.getNumberOfFeatureMaps()
        self._softmaxTemperature = softmaxTemperature
        
        self._setBlocksInputAttributes(layerConnected.outputTrain, layerConnected.outputVal, layerConnected.outputTest,
                                        layerConnected.outputShapeTrain, layerConnected.outputShapeVal, layerConnected.outputShapeTest)
        
        # At this last classification layer, the conv output needs to have bias added before the softmax.
        # NOTE: So, two biases are associated with this layer. self.b which is added in the ouput of the previous layer's output of conv,
        # and this self._bClassLayer that is added only to this final output before the softmax.
        (self._b,
        biasedInputToSoftmaxTrain,
        biasedInputToSoftmaxVal,
        biasedInputToSoftmaxTest) = makeBiasParamsAndApplyToFms( self.inputTrain, self.inputVal, self.inputTest, self._numberOfOutputClasses )
        self.params = self.params + [self._b]
        
        # ============ Softmax ==============
        #self.p_y_given_x_2d_train = ? Can I implement negativeLogLikelihood without this ?
        ( self.p_y_given_x_train,
        self.y_pred_train ) = applySoftmaxToFmAndReturnProbYandPredY( biasedInputToSoftmaxTrain, self.inputShapeTrain, self._numberOfOutputClasses, softmaxTemperature)
        ( self.p_y_given_x_val,
        self.y_pred_val ) = applySoftmaxToFmAndReturnProbYandPredY( biasedInputToSoftmaxVal, self.inputShapeVal, self._numberOfOutputClasses, softmaxTemperature)
        ( self.p_y_given_x_test,
        self.y_pred_test ) = applySoftmaxToFmAndReturnProbYandPredY( biasedInputToSoftmaxTest, self.inputShapeTest, self._numberOfOutputClasses, softmaxTemperature)
        
        self._setBlocksOutputAttributes(self.p_y_given_x_train, self.p_y_given_x_val, self.p_y_given_x_test, self.inputShapeTrain, self.inputShapeVal, self.inputShapeTest)
        
        layerConnected.setTargetBlock(self)
        
        
    def negativeLogLikelihood(self, y, weightPerClass):
        # Used in training.
        # param y: y = T.itensor4('y'). Dimensions [batchSize, r, c, z]
        # weightPerClass is a vector with 1 element per class.
        
        #Weighting the cost of the different classes in the cost-function, in order to counter class imbalance.
        e1 = np.finfo(np.float32).tiny
        addTinyProbMatrix = T.lt(self.p_y_given_x_train, 4*e1) * e1
        
        weightPerClassBroadcasted = weightPerClass.dimshuffle('x', 0, 'x', 'x', 'x')
        log_p_y_given_x_train = T.log(self.p_y_given_x_train + addTinyProbMatrix) #added a tiny so that it does not go to zero and I have problems with nan again...
        weighted_log_p_y_given_x_train = log_p_y_given_x_train * weightPerClassBroadcasted
        # return -T.mean( weighted_log_p_y_given_x_train[T.arange(y.shape[0]), y] )
        
        # Not a very elegant way to do the indexing but oh well...
        indexDim0 = T.arange( weighted_log_p_y_given_x_train.shape[0] ).dimshuffle( 0, 'x','x','x')
        indexDim2 = T.arange( weighted_log_p_y_given_x_train.shape[2] ).dimshuffle('x', 0, 'x','x')
        indexDim3 = T.arange( weighted_log_p_y_given_x_train.shape[3] ).dimshuffle('x','x', 0, 'x')
        indexDim4 = T.arange( weighted_log_p_y_given_x_train.shape[4] ).dimshuffle('x','x','x', 0)
        return -T.mean( weighted_log_p_y_given_x_train[ indexDim0, y, indexDim2, indexDim3, indexDim4] )
    
    
    def meanErrorTraining(self, y):
        # Returns float = number of errors / number of examples of the minibatch ; [0., 1.]
        # param y: y = T.itensor4('y'). Dimensions [batchSize, r, c, z]
        
        # check if y has same dimension of y_pred
        checkDimsOfYpredAndYEqual(y, self.y_pred_train, "training")
        
        #Mean error of the training batch.
        tneq = T.neq(self.y_pred_train, y)
        meanError = T.mean(tneq)
        return meanError
    
    def meanErrorValidation(self, y):
        # y = T.itensor4('y'). Dimensions [batchSize, r, c, z]
        
        # check if y has same dimension of y_pred
        checkDimsOfYpredAndYEqual(y, self.y_pred_val, "validation")
        
        # check if y is of the correct datatype
        if y.dtype.startswith('int'):
            # the T.neq operator returns a vector of 0s and 1s, where 1
            # represents a mistake in prediction
            tneq = T.neq(self.y_pred_val, y)
            meanError = T.mean(tneq)
            return meanError #The percentage of the predictions that is not the correct class.
        else:
            raise NotImplementedError()
        
    def getRpRnTpTnForTrain0OrVal1(self, y, training0OrValidation1):
        # The returned list has (numberOfClasses)x4 integers: >numberOfRealPositives, numberOfRealNegatives, numberOfTruePredictedPositives, numberOfTruePredictedNegatives< for each class (incl background).
        # Order in the list is the natural order of the classes (ie class-0 RP,RN,TPP,TPN, class-1 RP,RN,TPP,TPN, class-2 RP,RN,TPP,TPN ...)
        # param y: y = T.itensor4('y'). Dimensions [batchSize, r, c, z]
        
        yPredToUse = self.y_pred_train if  training0OrValidation1 == 0 else self.y_pred_val
        checkDimsOfYpredAndYEqual(y, yPredToUse, "training" if training0OrValidation1 == 0 else "validation")
        
        returnedListWithNumberOfRpRnTpTnForEachClass = []
        
        for class_i in xrange(0, self._numberOfOutputClasses) :
            #Number of Real Positive, Real Negatives, True Predicted Positives and True Predicted Negatives are reported PER CLASS (first for WHOLE).
            tensorOneAtRealPos = T.eq(y, class_i)
            tensorOneAtRealNeg = T.neq(y, class_i)

            tensorOneAtPredictedPos = T.eq(yPredToUse, class_i)
            tensorOneAtPredictedNeg = T.neq(yPredToUse, class_i)
            tensorOneAtTruePos = T.and_(tensorOneAtRealPos,tensorOneAtPredictedPos)
            tensorOneAtTrueNeg = T.and_(tensorOneAtRealNeg,tensorOneAtPredictedNeg)
                    
            returnedListWithNumberOfRpRnTpTnForEachClass.append( T.sum(tensorOneAtRealPos) )
            returnedListWithNumberOfRpRnTpTnForEachClass.append( T.sum(tensorOneAtRealNeg) )
            returnedListWithNumberOfRpRnTpTnForEachClass.append( T.sum(tensorOneAtTruePos) )
            returnedListWithNumberOfRpRnTpTnForEachClass.append( T.sum(tensorOneAtTrueNeg) )
            
        return returnedListWithNumberOfRpRnTpTnForEachClass
    
    def predictionProbabilities(self) :
        return self.p_y_given_x_test
    
    