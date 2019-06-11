#!/usr/bin/env python

###############################################################################
# Copyright 2019 The Apollo Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
###############################################################################

""" Restore record file by replacing its video frames with image frames. """

import errno
import glob
import os
import shutil

from absl import app
from absl import flags
from absl import logging
import cv2

from cyber_py.record import RecordReader, RecordWriter
from modules.drivers.proto.sensor_image_pb2 import CompressedImage

flags.DEFINE_string('from_record', None, 'The source record file that needs to be restored.')
flags.DEFINE_string('to_record', None, 'The restored record file.')

VIDEO_CHANNELS = {
    '/apollo/sensor/camera/front_12mm/image/compressed': 'front12mm',
    '/apollo/sensor/camera/front_6mm/image/compressed': 'front6mm',
    '/apollo/sensor/camera/left_fisheye/image/compressed': 'left_fisheye',
    '/apollo/sensor/camera/right_fisheye/image/compressed': 'right_fisheye',
    '/apollo/sensor/camera/rear_6mm/image/compressed': 'rear6mm',
}

class VideoConverter(object):
    """Convert video into images."""
    def __init__(self, work_dir, topic):
        # Initial type of video frames that defined in apollo video drive proto
        # The initial frame has meta data information shared by the following tens of frames
        self.initial_frame_type = 1
        self.image_ids = []
        self.first_initial_found = False
        video_dir = os.path.join(work_dir, 'videos')
        self.video_file = os.path.join(video_dir, '{}.h265'.format(topic))
        self.image_dir = '{}_images'.format(self.video_file)
        makedirs(video_dir)
        makedirs(self.image_dir)
        self.frame_writer = open(self.video_file, 'w+')

    def close_writer(self):
        """Close the video frames writer"""
        self.frame_writer.close()

    def write_frame(self, py_message):
        """Write video frames into binary format file"""
        if not self.first_initial_found:
            proto = image_message_to_proto(py_message)
            if proto.frame_type != self.initial_frame_type:
                return
            self.first_initial_found = True
        self.frame_writer.write(py_message.message)
        self.image_ids.append(get_message_id(py_message.timestamp, py_message.topic))

    def decode(self):
        """Decode video file into images"""
        video_decoder_exe = '/apollo/bazel-bin/modules/drivers/video/tools/decode_video/video2jpg'
        return_code = os.system('{} --input_video={} --output_dir={}'.format(
            video_decoder_exe, self.video_file, self.image_dir))
        if return_code != 0:
            logging.error('Failed to execute video2jpg for video {}'.format(self.video_file))
            return False
        generated_images = sorted(glob.glob('{}/*.jpg'.format(self.image_dir)))
        if len(generated_images) != len(self.image_ids):
            logging.error('Mismatch between original {} and generated frames {}'.format(
                len(self.image_ids), len(generated_images)))
            return False
        for idx in range(len(generated_images)):
            os.rename(generated_images[idx], os.path.join(self.image_dir, self.image_ids[idx]))
        return True

    def move_images(self, overall_image_dir):
        """Move self's images to overall image dir"""
        for image_file in os.listdir(self.image_dir):
            shutil.move(os.path.join(self.image_dir, image_file),
                        os.path.join(overall_image_dir, image_file))

def restore_record(input_record, output_record):
    """Entrance of processing."""
    # Define working dirs that store intermediate results in the middle of processing
    work_dir = 'work_dir'
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)

    # Decode videos
    converters = {}
    for topic in VIDEO_CHANNELS:
        converters[topic] = VideoConverter(work_dir, topic)

    reader = RecordReader(input_record)
    for message in reader.read_messages():
        if message.topic in VIDEO_CHANNELS:
            converters[message.topic].write_frame(message)

    image_dir = os.path.join(work_dir, 'images')
    makedirs(image_dir)
    for topic in VIDEO_CHANNELS:
        converters[topic].close_writer()
        converters[topic].decode()
        converters[topic].move_images(image_dir)

    # Restore target record file
    writer = RecordWriter(0, 0)
    writer.open(output_record)
    topic_descs = {}
    counter = 0
    reader = RecordReader(input_record)
    for message in reader.read_messages():
        message_content = message.message
        if message.topic in VIDEO_CHANNELS:
            message_content = retrieve_image(image_dir, message)
            if not message_content:
                continue
        counter += 1
        if counter % 1000 == 0:
            logging.info('rewriting {} th message to record {}'.format(counter, output_record))
        writer.write_message(message.topic, message_content, message.timestamp)
        if message.topic not in topic_descs:
            topic_descs[message.topic] = reader.get_protodesc(message.topic)
            writer.write_channel(message.topic, message.data_type, topic_descs[message.topic])
    writer.close()

    logging.info('All Done, converted record: {}'.format(output_record))

def retrieve_image(image_dir, message):
    """Actually change the content of message from video bytes to image bytes"""
    message_id = get_message_id(message.timestamp, message.topic)
    message_path = os.path.join(image_dir, message_id)
    if not os.path.exists(message_path):
        logging.error('message {} not found in image dir'.format(message_id))
        return None
    img_bin = cv2.imread(message_path)
    # Check by using NoneType explicitly to avoid ambitiousness
    if img_bin is None:
        logging.error('failed to read original message: {}'.format(message_path))
        return None
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 95]
    result, encode_img = cv2.imencode('.jpg', img_bin, encode_param)
    if not result:
        logging.error('failed to encode message {}'.format(message_id))
        return None
    message_proto = image_message_to_proto(message)
    message_proto.format = '; jpeg compressed bgr8'
    message_proto.data = message_proto.data.replace(message_proto.data[:], bytearray(encode_img))
    return message_proto.SerializeToString()

def get_message_id(timestamp, topic):
    """Unify the way to get an unique identifier for the given message"""
    return '{}{}'.format(timestamp, topic.replace('/', '_'))

def image_message_to_proto(py_message):
    """Message to prototype"""
    message_proto = CompressedImage()
    message_proto.ParseFromString(py_message.message)
    return message_proto

def makedirs(dir_path):
    """Make directories recursively."""
    if os.path.exists(dir_path):
        return
    try:
        os.makedirs(dir_path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            logging.error('Failed to makedir ' + dir_path)
            raise

def main(argv):
    """Main process."""
    if not flags.FLAGS.from_record or not os.path.exists(flags.FLAGS.from_record):
        logging.error('Please provide valid source record file.')
        return
    to_record = flags.FLAGS.to_record
    if not to_record:
        to_record = '{}_restored'.format(flags.FLAGS.from_record)
        logging.warn('The default restored record file is set as {}'.format(to_record))
    restore_record(flags.FLAGS.from_record, to_record)

if __name__ == '__main__':
    app.run(main)