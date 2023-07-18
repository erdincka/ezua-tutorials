import os
import shutil
from datetime import datetime, timedelta
from os import path
from airflow import DAG
from airflow.decorators import task
from airflow.models.param import Param
from airflow.utils.dates import days_ago
from airflow.operators.python import get_current_context
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.mysql.hooks.mysql import MySqlHook


default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': days_ago(1),
    'email': ['airflow@example.com'],
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 0
}
today = datetime.today().strftime('%Y-%m-%d')
with DAG(
    'data-extraction',
    default_args=default_args,
    schedule_interval=None,
    tags=['DF', 'S3', 'MySQL'],
    params={
        's3_bucket_name': Param("bank", type="string"),
        's3_files_prefix': Param(f"bank{today}.csv", type="string"),
        'result_path_in_shared_volume': Param("exported_by_airflow", type="string"),
        'result_path_prefix_s3': Param("from_minio", type="string"),
        'mysql_tables_list': Param(f"`bank{today}`", type="string"),
        'result_path_prefix_mysql': Param("from_mysql", type="string")
    },
) as dag:

    @task
    def cleanup_export_dir():
        context = get_current_context()
        global_result_path = context['params']['result_path_in_shared_volume']
        export_path = path.join("/mnt/shared", global_result_path)
        if path.exists(export_path):
            shutil.rmtree(export_path)
            return f"Deleted directory {export_path}"
        return "Nothing to clean"

    @task
    def get_all_filepaths_from_s3_path():
        context = get_current_context()
        bucket_name = context['params']['s3_bucket_name']
        prefix = context['params']['s3_files_prefix']
        s3=S3Hook(aws_conn_id="df_s3")
        filepaths = s3.list_keys(bucket_name=bucket_name, prefix=prefix)
        return filepaths

    @task
    def download_s3_file_to_shared_volume(filepath):
        context = get_current_context()
        bucket_name = context['params']['s3_bucket_name']
        global_result_path = context['params']['result_path_in_shared_volume']
        s3_prefix_path = context['params']['result_path_prefix_s3']
        prefix = context['params']['s3_files_prefix']
        subdir_in_s3 = path.dirname(filepath)
        local_path = path.join("/mnt/shared", global_result_path, s3_prefix_path,subdir_in_s3)
        os.umask(0o000)
        path_in_shared_volume = S3Hook(aws_conn_id="df_s3").download_file(
            bucket_name=bucket_name,
            key=filepath,
            local_path=local_path,
            preserve_file_name=True,
            use_autogenerated_subdir=False
        )
        os.chmod(path_in_shared_volume, 0o777)
        return path_in_shared_volume

    @task
    def split_mysql_tables_from_str():
        context = get_current_context()
        mysql_tables_list = context['params']['mysql_tables_list']
        return mysql_tables_list.split(',')

    @task
    def export_mysql_table_to_csv_shared_volume(table):
        context = get_current_context()
        global_result_path = context['params']['result_path_in_shared_volume']
        mysql_prefix_path = context['params']['result_path_prefix_mysql']
        table_filename = f"{table}.csv"
        base_csv_path = path.join("/mnt/shared", global_result_path, mysql_prefix_path)
        csv_path_real = path.join(base_csv_path, table_filename)
        os.umask(0o000)
        table_df = MySqlHook(mysql_conn_id="mysql_bank").get_pandas_df(f"SELECT * FROM {table}")
        os.makedirs(base_csv_path, mode=0o777, exist_ok=True)
        table_df.to_csv(csv_path_real,index=False)
        os.chmod(csv_path_real, 0o777)
        return csv_path_real

    cleanup_export_dir_task = cleanup_export_dir()

    cleanup_export_dir_task >> download_s3_file_to_shared_volume.expand(filepath=get_all_filepaths_from_s3_path())
    cleanup_export_dir_task >> export_mysql_table_to_csv_shared_volume.expand(table=split_mysql_tables_from_str())