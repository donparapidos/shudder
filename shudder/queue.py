# Copyright 2014 Scopely, Inc.
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

"""Module for setting up an sqs queue subscribed to
an sns topic polling for messages pertaining to our
impending doom.

"""
import json
import boto3
import hashlib
import logging

from shudder.config import CONFIG,LOG_FILE
import shudder.metadata as metadata

logging.basicConfig(filename=LOG_FILE,format='%(asctime)s %(levelname)s:%(message)s',level=logging.INFO)
INSTANCE_ID = metadata.get_instance_id()
QUEUE_NAME = "{prefix}-{id}".format(prefix=CONFIG['sqs_prefix'],
                                    id=INSTANCE_ID)


def create_queue():
    """Creates the SQS queue and returns the queue url and metadata"""
    logging.info('Connecting to sqs ' + QUEUE_NAME + 'on region ' + CONFIG['region'])
    conn = boto3.client('sqs', region_name=CONFIG['region'])
    queue_metadata = conn.create_queue(QueueName=QUEUE_NAME, Attributes={'VisibilityTimeout':'3600'})

    if 'queue_tags' in CONFIG:
        logging.info('Creating SQS tags: ' + CONFIG['queue_tags'])
        conn.tag_queue(QueueUrl=queue_metadata['QueueUrl'], Tags=CONFIG['queue_tags'])

    """Get the SQS queue object from the queue URL"""
    sqs = boto3.resource('sqs', region_name=CONFIG['region'])
    queue = sqs.Queue(queue_metadata['QueueUrl'])
    logging.info('queue object: ' + queue)
    return conn, queue


def subscribe_sns(queue):
    """Attach a policy to allow incoming connections from SNS"""
    statement_id = hashlib.md5((CONFIG['sns_topic'] +  queue.attributes.get('QueueArn')).encode('utf-8')).hexdigest()
    statement_id_exists = False
    logging.info('Statement ID: ' + statement_id)
    existing_policy = queue.attributes.get('Policy')
    if existing_policy:
        policy = json.loads(existing_policy)
    else:
        policy = {}
    logging.info('Policy: ' + policy)
    if 'Version' not in policy:
        policy['Version'] = '2008-10-17'
    if 'Statement' not in policy:
        policy['Statement'] = []
    # See if a Statement with the Sid exists already.
    for statement in policy['Statement']:
        if statement['Sid'] == statement_id:
           statement_id_exists = True
    if not statement_id_exists:
        statement = {'Action': 'SQS:SendMessage',
            'Effect': 'Allow',
            'Principal': {'AWS': '*'},
            'Resource': queue.attributes.get('QueueArn'),
            'Sid': statement_id,
            'Condition': {"ForAllValues:ArnEquals":{"aws:SourceArn":CONFIG['sns_topic']}}}
        policy['Statement'].append(statement)
    logging.info('Policy:' + policy)
    queue.set_attributes(Attributes={'Policy':json.dumps(policy)})
    """Subscribes the SNS topic to the queue."""
    logging.info('Subscribing the SNS topic to the queue')
    conn = boto3.client('sns', region_name=CONFIG['region'])
    sub = conn.subscribe(TopicArn=CONFIG['sns_topic'], Protocol='sqs', Endpoint=queue.attributes.get('QueueArn'))
    sns_arn = sub['SubscriptionArn']
    logging.info('SNS:' + sns_arn)
    return conn, sns_arn


def should_terminate(msg):
    """Check if the termination message is about our instance"""
    first_box = json.loads(msg.body)
    logging.info('Message raw:' + first_box)
    message = json.loads(first_box['Message'])
    logging.info('SQS Message:' + message)
    termination_msg = 'autoscaling:EC2_INSTANCE_TERMINATING'

    if 'LifecycleTransition' in message and message['LifecycleTransition'] == termination_msg and INSTANCE_ID == message['EC2InstanceId']:
        logging.info('We need to terminate. Hasta la vista.')
        return message
    else:
        return None

def clean_up_sns(sns_conn, sns_arn, queue):
    """Clean up SNS subscription and SQS queue"""
    logging.info('Deleting SQS queue')
    queue.delete()
    logging.info('Unsubscribing SNS topic')
    sns_conn.unsubscribe(SubscriptionArn=sns_arn)


def record_lifecycle_action_heartbeat(message):
    """Let AWS know we're still in the process of shutting down"""
    logging.info('Recording lifecycle heartbeat')
    logging.info('LifecycleHookName: ' + message['LifecycleHookName'])
    logging.info('AutoScalingGroupName: ' + message['AutoScalingGroupName'])
    logging.info('LifecycleActionToken: ' + message['LifecycleActionToken'])
    logging.info('InstanceId: ' + message['EC2InstanceId'])
    conn = boto3.client('autoscaling', region_name=CONFIG['region'])
    conn.record_lifecycle_action_heartbeat(
        LifecycleHookName=message['LifecycleHookName'],
        AutoScalingGroupName=message['AutoScalingGroupName'],
        LifecycleActionToken=message['LifecycleActionToken'],
        InstanceId=message['EC2InstanceId'])


def complete_lifecycle_action(message):
    """Let AWS know it's safe to terminate the instance now"""
    logging.info('Completing lifecycle action')
    logging.info('LifecycleHookName: ' + message['LifecycleHookName'])
    logging.info('AutoScalingGroupName: ' + message['AutoScalingGroupName'])
    logging.info('LifecycleActionResult: CONTINUE')
    logging.info('InstanceId: ' + message['EC2InstanceId'])
    conn = boto3.client('autoscaling', region_name=CONFIG['region'])
    conn.complete_lifecycle_action(
        LifecycleHookName=message['LifecycleHookName'],
        AutoScalingGroupName=message['AutoScalingGroupName'],
        LifecycleActionToken=message['LifecycleActionToken'],
        LifecycleActionResult='CONTINUE',
        InstanceId=message['EC2InstanceId'])


def poll_queue(conn, queue):
    """Poll SQS until we get a termination message."""
    messages = queue.receive_messages()
    for message in messages:
        logging.info('Polling queue for message')
        logging.info('Polled message: ' + message)
        message.delete()
        return should_terminate(message)
    return False
