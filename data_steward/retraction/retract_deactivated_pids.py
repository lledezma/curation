# Python imports
import argparse
import logging
from concurrent.futures import TimeoutError as TOError

# Third party imports
import google.cloud.bigquery as gbq
import pandas as pd
from google.cloud.exceptions import GoogleCloudError

import constants.retraction.retract_deactivated_pids as consts
import resources
import retraction.retract_utils as ru
from common import JINJA_ENV, FITBIT_TABLES, CDM_TABLES
from constants import bq_utils as bq_consts
from constants.cdr_cleaner import clean_cdr as cdr_consts
# Project imports
from utils import bq, pipeline_logging, sandbox as sb

LOGGER = logging.getLogger(__name__)





def query_runner(client, query_dict):
    """
    Runs the query specified via query_dict

    :param client: BigQuery client
    :param query_dict: Query dictionary as used by cleaning rules
    :return: job_id: BigQuery job id for the query job
    :raises: RuntimeError, GoogleCloudError, TOError: If BigQuery job fails
    """
    job_config = gbq.job.QueryJobConfig()

    if query_dict.get(cdr_consts.DESTINATION_TABLE) is not None:
        destination_table = gbq.TableReference.from_string(
            f'{client.project}.'
            f'{query_dict[cdr_consts.DESTINATION_DATASET]}.'
            f'{query_dict[cdr_consts.DESTINATION_TABLE]}')
        job_config.destination = destination_table
        job_config.write_disposition = query_dict.get(cdr_consts.DISPOSITION,
                                                      bq_consts.WRITE_EMPTY)

    try:
        query_job = client.query(query=query_dict.get(cdr_consts.QUERY),
                                 job_config=job_config,
                                 job_id_prefix='deact_')
        job_id = query_job.job_id
        LOGGER.info(f'Running deactivation job {job_id}')

        query_job.result()
        if query_job.errors:
            raise RuntimeError(
                f"Job {query_job.job_id} failed with error {query_job.errors} for query"
                f"{query_dict[cdr_consts.QUERY]}")
    except (GoogleCloudError, TOError) as exp:
        LOGGER.exception(f"Error {exp} while running query"
                         f"{query_dict[cdr_consts.QUERY]}")
        raise exp
    return job_id


def fq_table_name_verification(fq_table_name):
    """
    Ensures fq_table_name is of the format 'project.dataset.table'

    :param fq_table_name: fully qualified BQ table name
    :return: fq_table_name if valid
    :raises: ArgumentTypeError if invalid
    """
    if fq_table_name.count('.') == 2:
        return fq_table_name
    message = f"{fq_table_name} should be of the form 'project.dataset.table'"
    raise argparse.ArgumentTypeError(message)


def fq_deactivated_table_verification(fq_table_name):
    """
    Ensures fq_table_name is of the format 'project.dataset.table'

    :param fq_table_name: fully qualified BQ table name
    :return: fq_table_name if valid
    :raises: ArgumentTypeError if invalid
    """
    fq_table_name = fq_table_name_verification(fq_table_name)
    if fq_table_name.split('.')[-1] == consts.DEACTIVATED_PARTICIPANTS:
        return fq_table_name
    message = f"{fq_table_name} should be of the form 'project.dataset.{consts.DEACTIVATED_PARTICIPANTS}'"
    raise argparse.ArgumentTypeError(message)


def get_base_parser():
    parser = argparse.ArgumentParser(
        description='Retracts deactivated participants',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '-p',
        '--project_id',
        action='store',
        dest='project_id',
        help=
        'Identifies the project containing the dataset(s) to retract data from',
        required=True)
    parser.add_argument('-s',
                        '--console_log',
                        dest='console_log',
                        action='store_true',
                        help='Send logs to console')
    return parser


def get_parser():
    parser = get_base_parser()
    parser.add_argument('-d',
                        '--dataset_ids',
                        action='store',
                        nargs='+',
                        dest='dataset_ids',
                        help='Identifies datasets to target. Set to '
                        '"all_datasets" to target all datasets in project '
                        'or specific datasets as -d dataset_1 dataset_2 etc.',
                        required=True)
    parser.add_argument('-a',
                        '--fq_deact_table',
                        action='store',
                        dest='fq_deact_table',
                        type=fq_table_name_verification,
                        help='Specify fully qualified deactivated table '
                        'as "project.dataset.table"',
                        required=True)
    parser.add_argument('-r',
                        '--fq_pid_rid_table',
                        action='store',
                        dest='fq_pid_rid_table',
                        type=fq_table_name_verification,
                        help='Specify fully qualified pid-rid mapping table '
                        'as "project.dataset.table"')
    return parser


def run_deactivation(client,
                     project_id,
                     dataset_ids,
                     fq_deact_table,
                     fq_pid_rid_table=None):
    """
    Runs the deactivation retraction pipeline for a dataset

    :param client: BigQuery client
    :param project_id: Identifies the BigQuery project
    :param dataset_ids: Identifies the datasets to retract deactivated participants from
    :param fq_deact_table: Fully qualified table containing deactivated participants
        and deactivated dates as 'project.dataset.table'
    :param fq_pid_rid_table: Fully qualified table containing mappings from person_ids
        to research_ids as 'project.dataset.table'
    :return:job_ids: List of BigQuery job ids to perform the retraction as strings
    """
    pid_rid_table_ref = gbq.TableReference.from_string(
        fq_pid_rid_table) if fq_pid_rid_table else None
    deact_table_ref = gbq.TableReference.from_string(fq_deact_table)
    job_ids = {}
    for dataset_id in dataset_ids:
        job_ids[dataset_id] = []
        LOGGER.info(f"Retracting deactivated participants from '{dataset_id}'")
        sandbox_dataset_id = sb.check_and_create_sandbox_dataset(
            project_id, dataset_id)
        LOGGER.info(
            f"Using sandbox dataset '{sandbox_dataset_id}' for '{dataset_id}'")
        queries = generate_queries(client, project_id, dataset_id,
                                   sandbox_dataset_id, deact_table_ref,
                                   pid_rid_table_ref)
        for query in queries:
            job_id = query_runner(client, query)
            job_ids[dataset_id].append(job_id)
        LOGGER.info(
            f"Successfully retracted from {dataset_id} via jobs {job_ids[dataset_id]}"
        )
    return job_ids


def main(args=None):
    pipeline_logging.configure(logging.DEBUG, add_console_handler=True)
    parser = get_parser()
    args = parser.parse_args(args)
    client = bq.get_client(args.project_id)
    dataset_ids = ru.get_datasets_list(args.project_id, args.dataset_ids)
    LOGGER.info(
        f"Datasets to retract deactivated participants from: {dataset_ids}")
    run_deactivation(client, args.project_id, dataset_ids, args.fq_deact_table,
                     args.fq_pid_rid_table)
    LOGGER.info(
        f"Retraction of deactivated participants from {dataset_ids} complete")


if __name__ == '__main__':
    main()
