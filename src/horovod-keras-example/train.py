import argparse
import os
import pathlib

import numpy as np
import tensorflow as tf
import tensorflow.keras as keras
import horovod.tensorflow.keras as hvd


parser = argparse.ArgumentParser(description="Horovod + Keras distributed training benchmark")
parser.add_argument("--data-dir",
                    type=str,
                    help="Path to ILSVR data")
parser.add_argument("--shuffle-buffer-size",
                    type=int,
                    default=12811,
                    help="Size of the shuffle buffer (default buffer size 1% of all training images)")
parser.add_argument("--prefetch-buffer-size",
                    type=int,
                    default=1,
                    help="Size of the prefetch buffer")
parser.add_argument("--logging-dir",
                    type=str,
                    help="Path to the logging directory")

# Default settings from https://arxiv.org/abs/1706.02677.
parser.add_argument("--batch-size",
                    type=int,
                    default=32,
                    help="input batch size for training")
parser.add_argument("--val-batch-size",
                    type=int,
                    default=32,
                    help="input batch size for validation")
parser.add_argument("--warmup-epochs",
                    type=float,
                    default=5,
                    help="number of warmup epochs")
parser.add_argument("--epochs",
                    type=int,
                    default=90,
                    help="number of epochs to train")
parser.add_argument("--base-lr",
                    type=float,
                    default=1.25e-2,
                    help="learning rate for a single GPU")
parser.add_argument("--momentum",
                    type=float,
                    default=0.9,
                    help="SGD momentum")
parser.add_argument("--weight-decay",
                    type=float,
                    default=5e-5,
                    help="weight decay")
parser.add_argument("--seed",
                    type=int,
                    default=42,
                    help="random seed")
args = parser.parse_args()

hvd.init()
tf.random.set_seed(args.seed)

# Pin GPU to be used to process local rank (one GPU per process)
gpus = tf.config.experimental.list_physical_devices("GPU")
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
if gpus:
    tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], "GPU")

# define the data and logging directories
data_dir = pathlib.Path(args.data_dir)
training_data_dir = data_dir / "train"
validation_data_dir = data_dir / "val"
testing_data_dir = data_dir / "test"

# only log from first worker to avoid logging data corruption
verbose = 2 if hvd.rank() == 0 else 0
logging_dir = pathlib.Path(args.logging_dir)

checkpoints_logging_dir = logging_dir / "checkpoints"
if not os.path.isdir(checkpoints_logging_dir):
    os.mkdir(checkpoints_logging_dir)

tensorboard_logging_dir = logging_dir / "tensorboard"
if not os.path.isdir(tensorboard_logging_dir):
    os.mkdir(tensorboard_logging_dir)

# define constants used in data preprocessing
img_width, img_height = 224, 224
n_training_images = 1281167
n_validation_images = 50000
n_testing_images = 100000
class_names = tf.constant([item.name for item in training_data_dir.glob('*')])

@tf.function
def _get_label(file_path) -> tf.Tensor:
    # convert the path to a list of path components
    split_file_path = (tf.strings
                         .split(file_path, '/'))
    # The second to last is the class-directory
    label = tf.equal(split_file_path[-2], class_names)
    return label

@tf.function
def _decode_img(img):
    # convert the compressed string to a 3D uint8 tensor
    img = (tf.image
             .decode_jpeg(img, channels=3))
    # convert to floats in the [0,1] range.
    img = (tf.image
             .convert_image_dtype(img, tf.float32))
    # resize the image to the desired size.
    img = (tf.image
             .resize(img, [img_width, img_height]))
    return img

@tf.function
def preprocess(image):
    label = _get_label(image)
    # load the raw data from the file as a string
    img = tf.io.read_file(image)
    img = _decode_img(img)
    return img, label

# allow Tensorflow to choose the amount of parallelism used in preprocessing based on number of available CPUs
AUTOTUNE = (tf.data
              .experimental
              .AUTOTUNE)

# make sure that each GPU uses a different seed so that each GPU trains on different random sample of training data
training_dataset = (tf.data
                      .Dataset
                      .list_files(f"{training_data_dir}/*/*", shuffle=True, seed=hvd.rank())
                      .map(preprocess, num_parallel_calls=AUTOTUNE)
                      .shuffle(args.shuffle_buffer_size, reshuffle_each_iteration=True, seed=hvd.rank())
                      .repeat()
                      .batch(args.batch_size)
                      .prefetch(args.prefetch_buffer_size))

validation_dataset = (tf.data
                        .Dataset
                        .list_files(f"{validation_data_dir}/*/*", shuffle=False)
                        .map(preprocess, num_parallel_calls=AUTOTUNE)
                        .batch(args.val_batch_size))

# Look for a pre-existing checkpoint from which to resume training
checkpoint_filepath = None
initial_epoch = 0
for _epoch in range(args.epochs, 0, -1):
    _checkpoint_filepath = f"{checkpoints_logging_dir}/checkpoint-epoch-{_epoch:02d}.h5"
    if os.path.exists(_checkpoint_filepath):
        checkpoint_filepath = _checkpoint_filepath
        initial_epoch = _epoch
        break
print(initial_epoch)
hvd.broadcast(initial_epoch, root_rank=0, name='initial_epoch')

# If checkpoint exists, then restore on the first worker (and broadcast weights to other workers)
if checkpoint_filepath is not None and hvd.rank() == 0:
    print(checkpoint_filepath)
    model_fn = hvd.load_model(checkpoint_filepath)
else:    
    model_fn = (keras.applications
                     .ResNet50(weights=None, include_top=True))
    
    _loss_fn = (keras.losses
                     .CategoricalCrossentropy())
    
    # adjust initial learning rate for optimizer based on number of GPUs.
    _initial_lr = args.base_lr * hvd.size() 
    _optimizer = (keras.optimizers
                       .SGD(lr=_initial_lr, momentum=args.momentum))
    _distributed_optimizer = hvd.DistributedOptimizer(_optimizer)

    _metrics = [
        keras.metrics.CategoricalAccuracy(),
        keras.metrics.TopKCategoricalAccuracy(k=5)
    ]

    model_fn.compile(loss=_loss_fn,
                     optimizer=_distributed_optimizer,
                     metrics=_metrics,
                     experimental_run_tf_function=False, # required for Horovod to work with TF 2.0
                    )

_callbacks = [
    # Broadcast initial variable states from rank 0 worker to all other processes.
    #
    # This is necessary to ensure consistent initialization of all workers when
    # training is started with random weights or restored from a checkpoint.
    hvd.callbacks.BroadcastGlobalVariablesCallback(0),

    # Average metrics among workers at the end of every epoch.
    #
    # This callback must be in the list before the ReduceLROnPlateau,
    # TensorBoard, or other metrics-based callbacks.
    hvd.callbacks.MetricAverageCallback(),
    
    # Using `lr = 1.0 * hvd.size()` from the very beginning leads to worse final
    # accuracy. Scale the learning rate `lr = 1.0` ---> `lr = 1.0 * hvd.size()` during
    # the first five epochs. See https://arxiv.org/abs/1706.02677 for details.
    hvd.callbacks.LearningRateWarmupCallback(warmup_epochs=args.warmup_epochs, verbose=verbose),

    # After the warmup reduce learning rate by 10 on the 30th, 60th and 80th epochs.
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=args.warmup_epochs, end_epoch=30, multiplier=1.),
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=30, end_epoch=60, multiplier=1e-1),
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=60, end_epoch=80, multiplier=1e-2),
    hvd.callbacks.LearningRateScheduleCallback(start_epoch=80, multiplier=1e-3),
]

# Logging callbacks only on the rank 0 worker to prevent other workers from corrupting them.
if hvd.rank() == 0:
    _checkpoints_logging = (keras.callbacks
                                 .ModelCheckpoint(f"{checkpoints_logging_dir}/checkpoint-epoch-{{epoch:02d}}.h5",
                                                  save_best_only=False,
                                                  save_freq="epoch"))
    _tensorboard_logging = (keras.callbacks
                                 .TensorBoard(tensorboard_logging_dir))
    _callbacks.extend([_checkpoints_logging, _tensorboard_logging])
    

# model training loop
model_fn.fit(training_dataset,
             epochs=args.epochs,
             initial_epoch=initial_epoch,
             steps_per_epoch= 10, #n_training_images // (args.batch_size * hvd.size()),
             validation_data=validation_dataset,
             validation_steps=10, #n_validation_images // (args.val_batch_size * hvd.size()),
             verbose=verbose,
             callbacks=_callbacks)