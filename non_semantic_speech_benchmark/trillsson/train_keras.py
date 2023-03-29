# coding=utf-8
# Copyright 2023 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Trains on embeddings using Keras.
"""

from absl import app
from absl import flags
from absl import logging

import tensorflow as tf


from non_semantic_speech_benchmark.trillsson import get_data
from non_semantic_speech_benchmark.trillsson import models

FLAGS = flags.FLAGS

# Data config flags.
flags.DEFINE_list('file_patterns', None, 'Dataset location.')
flags.DEFINE_string('samples_key', None, 'Samples name.')
flags.DEFINE_string(
    'target_key', None, 'Teacher embedding key in precomputed tf.Examples.')

# Student network config flags.
flags.DEFINE_string('model_type', None, 'Specification for student model.')
flags.DEFINE_alias('mt', 'model_type')

# Training config flags.
flags.DEFINE_integer('train_batch_size', 1, 'Hyperparameter: batch size.')
flags.DEFINE_alias('tbs', 'train_batch_size')
flags.DEFINE_integer('max_sample_length', 32000, 'Max samples length.')
flags.DEFINE_alias('msl', 'max_sample_length')
flags.DEFINE_integer('shuffle_buffer_size', None, 'shuffle_buffer_size')
flags.DEFINE_float('lr', 0.001, 'Hyperparameter: learning rate.')
flags.DEFINE_string('logdir', None,
                    'Path to directory where to store summaries.')
flags.DEFINE_integer('training_steps', 1000,
                     'The number of steps to run training for.')
flags.DEFINE_integer('measurement_store_interval', 10,
                     'The number of steps between storing objective value in '
                     'measurements.')
flags.DEFINE_integer(
    'checkpoint_max_to_keep', None,
    'Number of previous checkpoints to save to disk.'
    'Default (None) is to store all checkpoints.')
flags.DEFINE_integer('num_epochs', 50, 'Number of epochs to train for.')
flags.DEFINE_alias('e', 'num_epochs')



def train_and_report(debug=False, target_dim=1024):
  """Trains the classifier."""
  logging.info('Logdir: %s', FLAGS.logdir)
  logging.info('Batch size: %s', FLAGS.train_batch_size)

  reader = tf.data.TFRecordDataset
  target_key = FLAGS.target_key

  ds = get_data.get_data(
      file_patterns=FLAGS.file_patterns,
      output_dimension=target_dim,
      reader=reader,
      samples_key=FLAGS.samples_key,
      target_key=target_key,
      batch_size=FLAGS.train_batch_size,
      loop_forever=True,
      shuffle=True,
      max_samples_length=FLAGS.max_sample_length,
      shuffle_buffer_size=FLAGS.shuffle_buffer_size,
      samples_are_float=True)
  assert len(ds.element_spec) == 2, ds.element_spec
  ds.element_spec[0].shape.assert_has_rank(2)  # audio samples
  ds.element_spec[1].shape.assert_has_rank(2)  # teacher embeddings
  output_dimension = ds.element_spec[1].shape[1]
  assert output_dimension == target_dim, (output_dimension, target_dim)

  # Define loss and optimizer hyparameters.
  loss_obj = tf.keras.losses.MeanSquaredError(name='mse_loss')
  opt = tf.keras.optimizers.Adam(
      learning_rate=FLAGS.lr, beta_1=0.9, beta_2=0.999, epsilon=1e-8)
  global_step = opt.iterations
  # Create model, loss, and other objects.
  model = models.get_keras_model(
      model_type=FLAGS.model_type, frame_hop=FLAGS.frame_hop)
  assert model.trainable_variables
  # Add additional metrics to track.
  train_loss = tf.keras.metrics.MeanSquaredError(name='train_loss')
  train_mae = tf.keras.metrics.MeanAbsoluteError(name='train_mae')
  summary_writer = tf.summary.create_file_writer(FLAGS.logdir)
  train_step = get_train_step(
      model, loss_obj, opt, train_loss, train_mae, summary_writer)
  checkpoint = tf.train.Checkpoint(model=model, global_step=global_step)
  manager = tf.train.CheckpointManager(
      checkpoint, FLAGS.logdir, max_to_keep=FLAGS.checkpoint_max_to_keep)
  logging.info('Checkpoint prefix: %s', FLAGS.logdir)
  checkpoint.restore(manager.latest_checkpoint)

  if debug: return
  logging.info('Starting loop with tbs: %s', FLAGS.train_batch_size)
  for inputs, targets in ds:
    # Inputs are audio vectors.
    inputs.shape.assert_has_rank(2)
    inputs.shape.assert_is_compatible_with([FLAGS.train_batch_size, None])
    targets.shape.assert_has_rank(2)
    targets.shape.assert_is_compatible_with(
        [FLAGS.train_batch_size, target_dim])
    train_step(inputs, targets, global_step)
    # Optional print output and save model.
    if global_step % 10 == 0:
      logging.info('step: %i, train loss: %f, train mean abs error: %f',
                   global_step, train_loss.result(), train_mae.result())
    if global_step % FLAGS.measurement_store_interval == 0:
      manager.save(checkpoint_number=global_step)

  manager.save(checkpoint_number=global_step)
  logging.info('Finished training.')


def get_train_step(model, loss_obj, opt, train_loss, train_mae, summary_writer):
  """Returns a function for train step."""
  assert model.trainable_variables

  def train_step(wav_samples, targets, step):
    with tf.GradientTape() as tape:
      logits = model(wav_samples, training=True)['embedding']
      logits.shape.assert_is_compatible_with(targets.shape)
      loss_value = loss_obj(y_true=targets, y_pred=logits)
    # Grads and optimizer.
    grads = tape.gradient(loss_value, model.trainable_variables)
    opt.apply_gradients(zip(grads, model.trainable_variables))

    # Record loss.
    train_loss.update_state(y_pred=targets, y_true=logits)
    train_mae.update_state(y_pred=targets, y_true=logits)

    # Summaries.
    with summary_writer.as_default():
      tf.summary.scalar('mse_loss', loss_value, step=step)
      tf.summary.scalar('mse_loss_smoothed', train_loss.result(), step=step)
      tf.summary.scalar('mae', train_mae.result(), step=step)
  return train_step


def main(unused_argv):
  assert FLAGS.file_patterns
  assert FLAGS.shuffle_buffer_size
  assert FLAGS.logdir
  assert FLAGS.samples_key
  assert FLAGS.target_key

  assert tf.executing_eagerly()
  train_and_report()


if __name__ == '__main__':
  app.run(main)
