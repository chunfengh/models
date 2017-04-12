#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import os.path
import time
import tempfile
import tensorflow as tf
import StringIO

import random
import string
import mmap

from tensorflow.python.platform import gfile
from tensorflow.python.platform import tf_logging as logging

from tensorflow.python.saved_model import builder as saved_model_builder
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import signature_def_utils
from tensorflow.python.saved_model import tag_constants
from tensorflow.python.saved_model import utils

from google.protobuf import text_format

from syntaxnet import sentence_pb2
from syntaxnet import graph_builder
from syntaxnet import structured_graph_builder
from syntaxnet.ops import gen_parser_ops
from syntaxnet import task_spec_pb2

flags = tf.app.flags
FLAGS = flags.FLAGS


flags.DEFINE_string('task_context', '',
                    'Path to a task context with inputs and parameters for '
                    'feature extractors.')
flags.DEFINE_string('resource_dir', '',
                    'Optional base directory for task context resources.')
flags.DEFINE_string('model_path', '', 'Path to model parameters.')
flags.DEFINE_string('arg_prefix', None, 'Prefix for context parameters.')
flags.DEFINE_string('graph_builder', 'greedy',
                    'Which graph builder to use, either greedy or structured.')
flags.DEFINE_string('input', 'stdin',
                    'Name of the context input to read data from.')
flags.DEFINE_string('output', 'stdout',
                    'Name of the context input to write data to.')
flags.DEFINE_string('hidden_layer_sizes', '200,200',
                    'Comma separated list of hidden layer sizes.')
flags.DEFINE_integer('batch_size', 32,
                     'Number of sentences to process in parallel.')
flags.DEFINE_integer('beam_size', 8, 'Number of slots for beam parsing.')
flags.DEFINE_integer('max_steps', 1000, 'Max number of steps to take.')
flags.DEFINE_bool('slim_model', False,
                  'Whether to expect only averaged variables.')



MODEL_DIR = '/home/chunfengh/models/syntaxnet/models/Chinese'
USE_SLIM_MODEL = True
BATCH_SIZE = 1024
BEAM_SIZE = 8
MAX_STEPS = 1000

TOKENIZER_TASK_CONTEXT = '/home/chunfengh/models/syntaxnet/syntaxnet/models/parsey_universal/context-tokenize-zh-tensor.pbtxt'
TOKENIZER_ARG_PREFIX = 'brain_tokenizer_zh'
TOKENIZER_HIDDEN_LAYER = '256,256'
TOKENIZER_MODEL_PATH = 'tokenizer-params'

TASK_CONTEXT = '/home/chunfengh/models/syntaxnet/syntaxnet/models/parsey_universal/context-tensor.pbtxt'
CONLL_INPUT = 'stdin-conll'
CONLL_OUTPUT = 'stdout-conll'
TOKENIZED_INPUT = 'stdin'
UNTOKENIZED_INPUT = 'stdin-untoken'

MORPHER_HIDDEN_LAYER = '64'
MORPHER_ARG_PREFIX = 'brain_morpher'
MORPHER_MODEL_PATH = 'morpher-params'
MORPHER_EXPORT_MODEL_DIR = '/home/chunfengh/models/syntaxnet/models/export/Chinese/morpher'

TAGGER_HIDDEN_LAYER = '64'
TAGGER_ARG_PREFIX = 'brain_tagger'
TAGGER_MODEL_PATH = 'tagger-params'
TAGGER_EXPORT_MODEL_DIR = '/home/chunfengh/models/syntaxnet/models/export/Chinese/tagger'

PARSER_HIDDEN_LAYER = '512,512'
PARSER_ARG_PREFIX = 'brain_parser'
PARSER_MODEL_PATH = 'parser-params'
PARSER_EXPORT_MODEL_DIR = '/home/chunfengh/models/syntaxnet/models/export/Chinese/parser'


def RewriteContext(task_context, in_corpus_name):
  context = task_spec_pb2.TaskSpec()
  with gfile.FastGFile(task_context, 'rb') as fin:
    text_format.Merge(fin.read(), context)
  tf_in = tempfile.NamedTemporaryFile(delete=False)
  for resource in context.input:
    for part in resource.part:
      if part.file_pattern not in ['-', 'tensor']:
        part.file_pattern = os.path.join(MODEL_DIR, part.file_pattern)
    if resource.name == in_corpus_name:
      for part in resource.part:
        if part.file_pattern == '-':
          part.file_pattern = tf_in.name
  fout = tempfile.NamedTemporaryFile(delete=False)
  fout.write(str(context))
  return fout.name, tf_in.name


def UnderscoreIfEmpty(part):
  if not part:
    return unicode('_')
  return unicode(part)


def GetMorphAttributes(token):
  extension = (sentence_pb2.TokenMorphology.morphology)
  if not token.HasExtension(extension):
    return unicode('_')
  morph = token.Extensions[extension]
  if not morph:
    return unicode('_')
  if len(morph.attribute) == 0:
    return unicode('_')
  attrs = []
  for attribute in morph.attribute:
    value = attribute.name
    if attribute.value != 'on':
      value += unicode('=')
      value += attribute.value
    attrs.append(value)
  return unicode('|').join(attrs);

  
def ConvertTokenToString(index, token):
  fields = []
  fields.append(unicode(index + 1))
  fields.append(UnderscoreIfEmpty(token.word))
  fields.append(unicode('_'))
  fields.append(UnderscoreIfEmpty(token.category))
  fields.append(UnderscoreIfEmpty(token.tag))
  fields.append(GetMorphAttributes(token))
  fields.append(unicode(token.head + 1))
  fields.append(UnderscoreIfEmpty(token.label))
  fields.append(unicode('_'))
  fields.append(unicode('_'))
  return unicode('\t').join(fields)

  
def ConvertToString(sentence):
  value = unicode('')
  lines = []
  for index in range(len(sentence.token)):
    lines.append(ConvertTokenToString(index, sentence.token[index]))
  return unicode('\n').join(lines) + unicode('\n\n')


def ConvertToString1(sentence):
  value = unicode('')
  for token in sentence.token:
    if value != u'':
      value += unicode(' ')
    value += token.word
    if token.HasField('tag'):
      value += unicode('_')
      value += token.tag
    if token.HasField('head'):
      value += unicode('_')
      value += unicode(token.head())
  value += unicode('\n')
  return value


class ParserEval:
  def __init__(self,
               sess,
               task_context,
               arg_prefix,
               hidden_layer_sizes,
               model_path,
               in_corpus_name,
               out_corpus_name):
    self.task_context, self.in_name = RewriteContext(task_context,
                                                     in_corpus_name)
    self.arg_prefix = arg_prefix
    self.sess = sess
    self.in_corpus_name = in_corpus_name
    self.out_corpus_name = out_corpus_name
    feature_sizes, domain_sizes, embedding_dims, num_actions = self.sess.run(
        gen_parser_ops.feature_size(task_context=self.task_context,
                                    arg_prefix=self.arg_prefix))
    self.feature_sizes = feature_sizes
    self.domain_sizes = domain_sizes
    self.embedding_dims = embedding_dims
    self.num_actions = num_actions
    self.hidden_layer_sizes = map(int, hidden_layer_sizes.split(','))

    with tf.variable_scope(arg_prefix):
      self.parser = structured_graph_builder.StructuredGraphBuilder(
          self.num_actions,
          self.feature_sizes,
          self.domain_sizes,
          self.embedding_dims,
          self.hidden_layer_sizes,
          gate_gradients=True,
          arg_prefix=self.arg_prefix,
          beam_size=BEAM_SIZE,
          max_steps=MAX_STEPS)
      self.parser.AddEvaluation(self.task_context,
                                BATCH_SIZE,
                                evaluation_max_steps=MAX_STEPS)
      self.parser.AddSaver(USE_SLIM_MODEL)
      self.sess.run(self.parser.inits.values())
      self.parser.saver.restore(self.sess, os.path.join(MODEL_DIR, model_path))
      self.document = tf.placeholder(tf.string, name='input') 
      self.parser.Predict(self.document,
                          self.task_context,
                          BATCH_SIZE,
                          corpus_name=self.in_corpus_name,
                          evaluation_max_steps=MAX_STEPS)
      self.sig_def = signature_def_utils.build_signature_def(
          inputs = {
            'input': utils.build_tensor_info(self.document)
          },
          outputs = {
              'epochs': utils.build_tensor_info(
                            self.parser.evaluation['epochs']),
              'eval_metrics': utils.build_tensor_info(
                                  self.parser.evaluation['eval_metrics']),
              'documents': utils.build_tensor_info(
                               self.parser.evaluation['documents'])
          })


  def Export(self, export_dir):
    builder = saved_model_builder.SavedModelBuilder(export_dir)
    builder.add_meta_graph_and_variables(
        self.sess,
        [tag_constants.SERVING],
        signature_def_map = {
            signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY: self.sig_def
        })
    builder.save()


def main(unused_argv):
  with tf.Graph().as_default(), tf.Session() as sess:
    #model = ParserEval(sess,
    #                   TASK_CONTEXT,
    #                   MORPHER_ARG_PREFIX,
    #                   MORPHER_HIDDEN_LAYER,
    #                   MORPHER_MODEL_PATH,
    #                   TOKENIZED_INPUT,
    #                   CONLL_OUTPUT)
    #model.Export(MORPHER_EXPORT_MODEL_DIR)
    #model = ParserEval(sess,
    #                   TASK_CONTEXT,
    #                   TAGGER_ARG_PREFIX,
    #                   TAGGER_HIDDEN_LAYER,
    #                   TAGGER_MODEL_PATH,
    #                   CONLL_INPUT,
    #                   CONLL_OUTPUT)
    #model.Export(TAGGER_EXPORT_MODEL_DIR)
    model = ParserEval(sess,
                       TASK_CONTEXT,
                       PARSER_ARG_PREFIX,
                       PARSER_HIDDEN_LAYER,
                       PARSER_MODEL_PATH,
                       CONLL_INPUT,
                       CONLL_OUTPUT)
    model.Export(PARSER_EXPORT_MODEL_DIR)



if __name__ == '__main__':
  tf.app.run()


