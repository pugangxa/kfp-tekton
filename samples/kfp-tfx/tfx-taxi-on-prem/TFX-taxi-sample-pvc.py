# Copyright 2019 kubeflow.org.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import tensorflow as tf

from typing import Text

from kfp import onprem
import kfp
from tfx.components.evaluator.component import Evaluator
from tfx.components.example_gen.csv_example_gen.component import CsvExampleGen
from tfx.components.example_validator.component import ExampleValidator
from tfx.components.model_validator.component import ModelValidator
from tfx.components.pusher.component import Pusher
from tfx.components.schema_gen.component import SchemaGen
from tfx.components.statistics_gen.component import StatisticsGen
from tfx.components.trainer.component import Trainer
from tfx.components.transform.component import Transform
from tfx.orchestration import metadata
from tfx.orchestration import pipeline
from tfx.orchestration.kubeflow import kubeflow_dag_runner
from tfx.proto import evaluator_pb2
from tfx.utils.dsl_utils import csv_input
from tfx.proto import pusher_pb2
from tfx.proto import trainer_pb2
from tfx.extensions.google_cloud_ai_platform.trainer import executor as ai_platform_trainer_executor
from ml_metadata.proto import metadata_store_pb2
from tfx.orchestration.kubeflow.proto import kubeflow_pb2

_output_bucket = '/mnt/shared'
_model_bucket = 's3://tfx-taxi'

def env_params(name='S3_ENDPOINT', value='https://s3.us.cloud-object-storage.appdomain.cloud'):
    def _env_params(task):
        from kubernetes import client as k8s_client
        return(task.add_env_variable(k8s_client.V1EnvVar(name=name, value=value)))
    return _env_params


s3_endpoint_params = env_params('S3_ENDPOINT', 'minio-service.kubeflow:9000')
s3_use_https_params = env_params('S3_USE_HTTPS', '0')
s3_verify_ssl_params = env_params('S3_VERIFY_SSL', '0')
access_key_id_params = env_params('AWS_ACCESS_KEY_ID', 'minio')
secret_access_key_params = env_params('AWS_SECRET_ACCESS_KEY', 'minio123')


mount_volume_op = onprem.mount_pvc('tfx-volume', 'shared-volume', _output_bucket)

def _create_test_pipeline(pipeline_root: Text, csv_input_location: Text,
                          taxi_module_file: Text, output_bucket: Text, enable_cache: bool):
  """Creates a simple Kubeflow-based Chicago Taxi TFX pipeline.

  Args:
    pipeline_name: The name of the pipeline.
    pipeline_root: The root of the pipeline output.
    csv_input_location: The location of the input data directory.
    taxi_module_file: The location of the module file for Transform/Trainer.
    enable_cache: Whether to enable cache or not.

  Returns:
    A logical TFX pipeline.Pipeline object.
  """

  examples = csv_input(csv_input_location)

  example_gen = CsvExampleGen(input_base=examples)
  statistics_gen = StatisticsGen(input_data=example_gen.outputs.examples)
  infer_schema = SchemaGen(
      stats=statistics_gen.outputs.output, infer_feature_shape=False)
  validate_stats = ExampleValidator(
      stats=statistics_gen.outputs.output, schema=infer_schema.outputs.output)
  transform = Transform(
      input_data=example_gen.outputs.examples,
      schema=infer_schema.outputs.output,
      module_file=taxi_module_file)
  trainer = Trainer(
      module_file=taxi_module_file,
      transformed_examples=transform.outputs.transformed_examples,
      schema=infer_schema.outputs.output,
      transform_output=transform.outputs.transform_output,
      train_args=trainer_pb2.TrainArgs(num_steps=10000),
      eval_args=trainer_pb2.EvalArgs(num_steps=5000))
  model_analyzer = Evaluator(
      examples=example_gen.outputs.examples,
      model_exports=trainer.outputs.output,
      feature_slicing_spec=evaluator_pb2.FeatureSlicingSpec(specs=[
          evaluator_pb2.SingleSlicingSpec(
              column_for_slicing=['trip_start_hour'])
      ]))
  model_validator = ModelValidator(
      examples=example_gen.outputs.examples, model=trainer.outputs.output)
  pusher = Pusher(
      model_export=trainer.outputs.output,
      model_blessing=model_validator.outputs.blessing,
      push_destination=pusher_pb2.PushDestination(
          filesystem=pusher_pb2.PushDestination.Filesystem(
              base_directory=os.path.join(output_bucket, 'model_serving'))))

  return pipeline.Pipeline(
      pipeline_name='chicago_taxi_pipeline_simple',
      pipeline_root=pipeline_root,
      components=[
          example_gen, statistics_gen, infer_schema, validate_stats, transform,
          trainer, model_analyzer, model_validator, pusher
      ],
      enable_cache=enable_cache,
  )


def _get_kubeflow_metadata_config() -> kubeflow_pb2.KubeflowMetadataConfig:
  config = kubeflow_pb2.KubeflowMetadataConfig()
  config.mysql_db_service_host.environment_variable = 'MYSQL_SERVICE_HOST'
  config.mysql_db_service_port.environment_variable = 'MYSQL_SERVICE_PORT'
  config.mysql_db_name.value = 'metadb'
  config.mysql_db_user.value = 'root'
  config.mysql_db_password.value = ''
  return config


if __name__ == '__main__':
  data_root = os.path.join(_output_bucket, 'data')
  taxi_module_file = os.path.join(_output_bucket, 'modules/taxi_utils.py')
  pipeline_root = os.path.join(_output_bucket, 'tfx_taxi_simple')
  enable_cache = False
  output_bucket = _model_bucket

  pipeline = _create_test_pipeline(
      pipeline_root, data_root, taxi_module_file, output_bucket, enable_cache=enable_cache)

  config = kubeflow_dag_runner.KubeflowDagRunnerConfig(
      pipeline_operator_funcs=[s3_endpoint_params, s3_use_https_params, s3_verify_ssl_params, access_key_id_params, secret_access_key_params, mount_volume_op],
      kubeflow_metadata_config=_get_kubeflow_metadata_config())

  kubeflow_dag_runner.KubeflowDagRunner(config=config).run(pipeline)
