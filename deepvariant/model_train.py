# Copyright 2017 Google Inc.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Trains the DeepVariant model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os



from tensorflow import flags
from absl import logging
import tensorflow as tf

from third_party.nucleus.util import proto_utils
from deepvariant import data_providers
from deepvariant import logging_level
from deepvariant import modeling

slim = tf.contrib.slim

FLAGS = flags.FLAGS

# Data set selection parameters
flags.DEFINE_string('dataset_config_pbtxt', None,
                    'The path to the dataset config file.')

flags.DEFINE_string('model_name', 'inception_v3',
                    'The name of the model to use for predictions.')

flags.DEFINE_integer('batch_size', 64, 'The number of samples in each batch.')

flags.DEFINE_string('master', '',
                    'The TensorFlow master to use. Set to the empty string '
                    'to let TF pick a default.')

flags.DEFINE_string('train_dir', '/tmp/deepvariant/',
                    'Directory where to write event logs.')

flags.DEFINE_boolean('use_tpu', False, 'use tpu if available')

flags.DEFINE_integer('worker_replicas', 1, 'Number of worker replicas.')

flags.DEFINE_integer(
    'ps_tasks', 0,
    'The number of parameter servers. If the value is 0, then the parameters '
    'are handled locally by the worker.')

flags.DEFINE_integer('task', 0, 'Task id of the replica running the training.')

flags.DEFINE_integer('number_of_steps', 30000000,
                     'Maximum number of global steps to take when training.')

flags.DEFINE_integer(
    'num_retries', 0,
    'The number of times to retry on InternalError or UnavailableError.')

# Pre-trained model parameters
flags.DEFINE_string(
    'start_from_checkpoint', 'model_default',
    'A path to a checkpoint of model weights to initalize our model at the '
    'start of training. If None or "", the model will start from random weights'
    '. The special value "model_default" will use the default pretrained '
    'path for the selected model.')

flags.DEFINE_integer('max_checkpoints_to_keep', 10,
                     'Number of last checkpoints to keep during training. '
                     'Passing "0" preserves all checkpoints.')


def loss(logits, one_hot_labels, label_smoothing):
  """Creates a loss function for training logits against one_hot_labels.

  Args:
      logits: tensor. logits of the model we want to train.
    one_hot_labels: One-hot encoded truth labels that we want to train this
      model to predict.
    label_smoothing: float. label_smoothing value for softmax_cross_entropy.

  Returns:
    A `Tensor` whose value represents the total loss.
  """
  slim.losses.softmax_cross_entropy(
      logits, one_hot_labels, label_smoothing=label_smoothing, weights=1.0)
  return slim.losses.get_total_loss()


def run(target, unused_is_chief, device_fn, use_tpu):
  """Run training.

  Args:
     target: The target of the TensorFlow standard server to use. Can be the
       empty string to run locally using an inprocess server.
     device_fn: Device function used to assign ops to devices.
     use_tpu: turn on tpu code path.
  """
  if not FLAGS.dataset_config_pbtxt:
    logging.error('Need to specify --dataset_config_pbtxt')
    return

  g = tf.Graph()
  with g.as_default():
    with tf.device(device_fn):
      # If ps_tasks is zero, the local device is used. When using multiple
      # (non-local) replicas, the ReplicaDeviceSetter distributes the variables
      # across the different devices.

      tf_dataset = data_providers.get_input_fn_from_dataset(
          dataset_config_filename=FLAGS.dataset_config_pbtxt,
          mode=tf.estimator.ModeKeys.TRAIN,
          use_tpu=use_tpu)
      model = modeling.get_model(FLAGS.model_name)
      logging.info('Running training on %s with model %s and tpu %s',
                   tf_dataset, FLAGS.model_name, use_tpu)

      batches_per_epoch = tf_dataset.num_examples // FLAGS.batch_size
      logging.info('Batches per epoch %s', batches_per_epoch)
      params = dict(batches_per_epoch=batches_per_epoch,)
      estimator = model.make_estimator(
          batch_size=FLAGS.batch_size,
          model_dir=FLAGS.train_dir,
          params=params,
          use_tpu=use_tpu,
          master=target,
      )
      estimator.train(
          input_fn=tf_dataset,
          max_steps=FLAGS.number_of_steps,
      )


def parse_and_run():
  """Parse TF_CONFIG to cluster_spec and call run().

  TF_CONFIG environment variable is available when running using
  gcloud either locally or on cloud. It has all the information required
  to create a ClusterSpec which is important for running distributed code.

  Raises:
    ValueError: If flags are invalid.
  """
  tf_config = os.environ.get('TF_CONFIG')
  logging.info('TF_CONFIG %s', tf_config)

  for name in ['master', 'task', 'ps_tasks']:
    if getattr(FLAGS, name) and tf_config:
      raise ValueError(
          'Either the flag --%s or the environment variable TF_CONFIG can be'
          ' set but not both.' % name)

  # redacted
  #
  # If TF_CONFIG is not available we are either running locally in Cloud
  # or distributed inside Google. On Cloud the default values of
  # FLAGS.master and FLAGS.task correspond to running training locally.
  # Inside Google they will be set as needed to configure local or distributed
  # training. Inside Google we don't need to explicitly set worker_device
  # in replica_device_setter becaue this will be set automatically based
  # on various flags.
  if not tf_config:
    device_fn = tf.train.replica_device_setter(FLAGS.ps_tasks)
    return run(
        FLAGS.master,
        FLAGS.task == 0,
        device_fn=device_fn,
        use_tpu=FLAGS.use_tpu)

  tf_config_json = json.loads(tf_config)

  cluster = tf_config_json.get('cluster')
  job_name = tf_config_json.get('task', {}).get('type')
  task_index = tf_config_json.get('task', {}).get('index')

  # If cluster information is empty run local
  if job_name is None or task_index is None:
    device_fn = tf.train.replica_device_setter(0)
    return run('', True, device_fn=device_fn, use_tpu=FLAGS.use_tpu)

  ps = cluster.get('ps', [])
  num_ps = len(ps)

  cluster_spec = tf.train.ClusterSpec(cluster)
  server = tf.train.Server(
      cluster_spec, job_name=job_name, task_index=task_index)

  if job_name == 'ps':
    server.join()
    return
  elif job_name in ['master', 'worker']:
    device_fn = tf.train.replica_device_setter(
        num_ps,
        worker_device='/job:%s/task:%d' % (job_name, task_index),
        cluster=cluster_spec)
    return run(
        server.target,
        job_name == 'master',
        device_fn=device_fn,
        use_tpu=FLAGS.use_tpu)


def main(_):
  """Run and handle retryable errors."""
  proto_utils.uses_fast_cpp_protos_or_die()

  logging_level.set_from_flag()
  for _ in range(FLAGS.num_retries + 1):
    try:
      parse_and_run()
      return
    except tf.errors.UnavailableError as e:
      # An UnavailableError indicates a gRPC error, typically this is
      # retryable.
      logging.error('Caught UnavailableError %s; will retry.', e)
    except tf.errors.InternalError as e:
      # Retry on an InternalError.
      logging.error('Caught InternalError %s; will retry.', e)


if __name__ == '__main__':
  tf.app.run()
