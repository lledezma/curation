# Python imports
import os
import unittest

# Third party imports
import mock

# Project imports
import app_identity
import bq_utils
import resources
from gcloud.gcs import StorageClient
from gcloud.bq import BigQueryClient
from tests import test_util
from constants.tools.create_combined_backup_dataset import EHR_CONSENT_TABLE_ID, RDR_TABLES_TO_COPY, DOMAIN_TABLES
from tools.create_combined_backup_dataset import (ehr_consent,
                                                  create_cdm_tables)
from resources import mapping_table_for

UNCONSENTED_EHR_COUNTS_QUERY = (
    '  select \'{domain_table}\' as table_id, count(1) as n from (SELECT DISTINCT'
    '  v.src_hpo_id AS src_hpo_id,'
    '  t.{domain_table}_id  AS {domain_table}_id'
    '  FROM `{ehr_dataset_id}.{domain_table}` AS t'
    '  JOIN `{ehr_dataset_id}._mapping_{domain_table}` AS v'
    '  ON t.{domain_table}_id = v.{domain_table}_id'
    '  WHERE NOT EXISTS'
    '  (SELECT 1 FROM `{combined_dataset_id}.{ehr_consent_table_id}` AS c'
    '  WHERE t.person_id = c.person_id))')


class CreateCombinedBackupDatasetTest(unittest.TestCase):
    project_id = app_identity.get_application_id()
    storage_client = StorageClient(project_id)
    bq_client = BigQueryClient(project_id)
    dataset_id = bq_utils.get_dataset_id()

    @classmethod
    def setUpClass(cls):
        print('**************************************************************')
        print(cls.__name__)
        print('**************************************************************')
        # TODO base class this
        ehr_dataset_id = bq_utils.get_dataset_id()
        rdr_dataset_id = bq_utils.get_rdr_dataset_id()
        test_util.delete_all_tables(cls.bq_client, ehr_dataset_id)
        test_util.delete_all_tables(cls.bq_client, rdr_dataset_id)
        test_util.setup_hpo_id_bucket_name_table(cls.bq_client, cls.dataset_id)
        cls.load_dataset_from_files(ehr_dataset_id,
                                    test_util.NYC_FIVE_PERSONS_PATH, True)
        cls.load_dataset_from_files(rdr_dataset_id, test_util.RDR_PATH)

    @classmethod
    @mock.patch("gcloud.gcs.LOOKUP_TABLES_DATASET_ID", dataset_id)
    def load_dataset_from_files(cls, dataset_id, path, mappings=False):
        hpo_bucket = cls.storage_client.get_hpo_bucket(test_util.FAKE_HPO_ID)
        cls.storage_client.empty_bucket(hpo_bucket)
        job_ids: list = []
        for table in resources.CDM_TABLES:
            job_ids.append(
                cls._upload_file_to_bucket(hpo_bucket, dataset_id, path, table))
            if mappings and table in DOMAIN_TABLES:
                mapping_table = f'_mapping_{table}'
                job_ids.append(
                    cls._upload_file_to_bucket(hpo_bucket, dataset_id, path,
                                               mapping_table))
        incomplete_jobs: list = bq_utils.wait_on_jobs(job_ids)
        if incomplete_jobs:
            message: str = f'Job id(s) {incomplete_jobs} failed to complete'
            raise RuntimeError(message)
        cls.storage_client.empty_bucket(hpo_bucket)

    @classmethod
    def _upload_file_to_bucket(cls, bucket, dataset_id: str, path: str,
                               table: str) -> str:

        filename: str = f'{table}.csv'
        filepath: str = os.path.join(path, filename)
        blob = bucket.blob(filename)
        try:
            blob.upload_from_filename(filepath)
        except FileNotFoundError:
            blob.upload_from_string('\n')
        gcs_path: str = f'gs://{bucket.name}/{filename}'
        load_results: dict = bq_utils.load_csv(table,
                                               gcs_path,
                                               cls.project_id,
                                               dataset_id,
                                               table,
                                               allow_jagged_rows=True)
        job_id: str = load_results['jobReference']['jobId']
        return job_id

    def setUp(self):
        self.ehr_dataset_id = bq_utils.get_dataset_id()
        self.rdr_dataset_id = bq_utils.get_rdr_dataset_id()
        self.combined_dataset_id = bq_utils.get_combined_dataset_id()
        test_util.delete_all_tables(self.bq_client, self.combined_dataset_id)

    def test_consented_person_id(self):
        """
        Test observation data has seven (7) persons with consent records as described below
         1: No
         2: Yes
         3: NULL
         4: No  followed by Yes
         5: Yes followed by No
         6: Yes followed by NULL
         7: NULL and Yes with same date/time
        """
        # sanity check
        # pre-conditions
        self.assertFalse(
            self.bq_client.table_exists(EHR_CONSENT_TABLE_ID,
                                        self.combined_dataset_id))

        # test
        ehr_consent(self.bq_client, self.rdr_dataset_id,
                    self.combined_dataset_id)

        # post conditions
        self.assertTrue(
            self.bq_client.table_exists(EHR_CONSENT_TABLE_ID,
                                        self.combined_dataset_id),
            'Table {dataset}.{table} created by consented_person'.format(
                dataset=self.combined_dataset_id, table=EHR_CONSENT_TABLE_ID))
        response = bq_utils.query('SELECT * FROM {dataset}.{table}'.format(
            dataset=self.combined_dataset_id, table=EHR_CONSENT_TABLE_ID))
        rows = bq_utils.response2rows(response)
        expected = {2, 4}
        actual = set(row['person_id'] for row in rows)
        self.assertSetEqual(
            expected, actual, 'Records in {dataset}.{table}'.format(
                dataset=self.combined_dataset_id, table=EHR_CONSENT_TABLE_ID))

    def test_copy_rdr_tables(self):
        for table in RDR_TABLES_TO_COPY:
            self.assertFalse(
                self.bq_client.table_exists(
                    table, self.combined_dataset_id))  # sanity check
            # self.bq_client.create_table(f'{self.combined_dataset_id}.{table}',
            #                             exists_ok=True)
            self.bq_client.copy_table(f'{self.rdr_dataset_id}.{table}',
                                      f'{self.combined_dataset_id}.{table}')
            actual = self.bq_client.table_exists(f'{table}')
            self.assertTrue(
                actual,
                msg='RDR table {table} should be copied'.format(table=table))

    def _ehr_only_records_excluded(self):
        """
        EHR person records which are missing from RDR are excluded from combined
        """
        query = ('WITH ehr_only AS '
                 ' (SELECT person_id '
                 '  FROM `{ehr_dataset_id}.person` AS ep '
                 '  WHERE NOT EXISTS '
                 '    (SELECT 1 '
                 '     FROM `{rdr_dataset_id}.person` AS rp '
                 '     WHERE rp.person_id = ep.person_id) '
                 ' ) '
                 'SELECT '
                 'ehr_only.person_id AS ehr_person_id, '
                 'p.person_id AS combined_person_id '
                 'FROM ehr_only '
                 'LEFT JOIN `{combined_dataset_id}.person` AS p '
                 'ON ehr_only.person_id = p.person_id').format(
                     ehr_dataset_id=self.ehr_dataset_id,
                     rdr_dataset_id=self.rdr_dataset_id,
                     combined_dataset_id=self.combined_dataset_id)
        response = bq_utils.query(query)
        rows = bq_utils.response2rows(response)
        self.assertGreater(len(rows), 0,
                           'Test data is missing EHR-only records')
        for row in rows:
            combined_person_id = row['combined_person_id']
            self.assertIsNone(
                combined_person_id,
                'EHR-only person_id `{ehr_person_id}` found in combined when it should be excluded'
            )

    def get_unconsented_ehr_records_count(self, table_name):
        query = UNCONSENTED_EHR_COUNTS_QUERY.format(
            rdr_dataset_id=bq_utils.get_rdr_dataset_id(),
            ehr_dataset_id=bq_utils.get_dataset_id(),
            combined_dataset_id=self.combined_dataset_id,
            domain_table=table_name,
            ehr_consent_table_id='_ehr_consent')
        response = bq_utils.query(query)
        rows = bq_utils.response2rows(response)
        return rows[0]['n']

    def _mapping_table_checks(self):
        """
        Check mapping tables exist, have correct schema, have expected number of records
        """
        where = (
            'WHERE EXISTS '
            '  (SELECT 1 FROM `{combined_dataset_id}.{ehr_consent_table_id}` AS c '
            '   WHERE t.person_id = c.person_id)').format(
                combined_dataset_id=self.combined_dataset_id,
                ehr_consent_table_id=EHR_CONSENT_TABLE_ID)
        ehr_counts = test_util.get_table_counts(self.ehr_dataset_id,
                                                DOMAIN_TABLES, where)
        rdr_counts = test_util.get_table_counts(self.rdr_dataset_id)
        combined_counts = test_util.get_table_counts(self.combined_dataset_id)
        output_tables = combined_counts.keys()
        expected_counts = dict()
        expected_diffs = ['observation']

        for table in DOMAIN_TABLES:
            expected_mapping_table = mapping_table_for(table)
            self.assertIn(expected_mapping_table, output_tables)
            expected_fields = resources.fields_for(expected_mapping_table)
            actual_table_info = bq_utils.get_table_info(
                expected_mapping_table, self.combined_dataset_id)
            actual_fields = actual_table_info.get('schema',
                                                  dict()).get('fields', [])
            actual_fields_norm = map(test_util.normalize_field_payload,
                                     actual_fields)
            self.assertCountEqual(expected_fields, actual_fields_norm)

            # Count should be sum of EHR and RDR
            # (except for tables like observation where extra records are created for demographics)
            if 'person_id' in [
                    field.get('name', '')
                    for field in resources.fields_for(table)
            ]:
                unconsented_ehr_records = self.get_unconsented_ehr_records_count(
                    table)
            else:
                unconsented_ehr_records = 0

            actual_count = combined_counts[expected_mapping_table]

            if table in expected_diffs:
                expected_count = actual_count
            else:
                expected_count = (ehr_counts[table] -
                                  unconsented_ehr_records) + rdr_counts[table]
            expected_counts[expected_mapping_table] = expected_count

        self.assertDictContainsSubset(expected_counts, combined_counts)

    def _all_rdr_records_included(self):
        """
        All rdr records are included whether or not there is corresponding ehr record
        """
        for domain_table in DOMAIN_TABLES:
            mapping_table = mapping_table_for(domain_table)
            query = (
                'SELECT rt.{domain_table}_id as id '
                'FROM `{rdr_dataset_id}.{domain_table}` AS rt '
                'LEFT JOIN `{combined_dataset_id}.{mapping_table}` AS m '
                'ON rt.{domain_table}_id = m.src_{domain_table}_id '
                'WHERE '
                '  m.{domain_table}_id IS NULL '
                'OR NOT EXISTS '
                ' (SELECT 1 FROM `{combined_dataset_id}.{domain_table}` AS t '
                '  WHERE t.{domain_table}_id = m.{domain_table}_id)').format(
                    domain_table=domain_table,
                    rdr_dataset_id=bq_utils.get_rdr_dataset_id(),
                    combined_dataset_id=bq_utils.get_combined_dataset_id(),
                    mapping_table=mapping_table)
            response = bq_utils.query(query)
            rows = bq_utils.response2rows(response)
            self.assertEqual(
                0, len(rows),
                "RDR records should map to records in mapping and combined tables"
            )

    def test_create_cdm_tables(self):
        # pre-conditions
        # Sanity check
        tables_before = self.bq_client.list_tables(self.combined_dataset_id)
        table_names_before = [table.table_id for table in tables_before]
        for table in resources.CDM_TABLES:
            self.assertNotIn(table, table_names_before)

        # test
        create_cdm_tables(self.bq_client, self.combined_dataset_id)

        # post conditions
        tables_after = self.bq_client.list_tables(self.combined_dataset_id)
        table_names_after = [table.table_id for table in tables_after]
        for table in resources.CDM_TABLES:
            self.assertIn(table, table_names_after)

    def _fact_relationship_loaded(self):
        # TODO
        # All fact_id_1 where domain_concept_id_1==21 map to measurement
        # All fact_id_2 where domain_concept_id_2==27 map to observation
        pass

    def tearDown(self):
        test_util.delete_all_tables(self.bq_client, self.combined_dataset_id)

    @classmethod
    def tearDownClass(cls):
        ehr_dataset_id = bq_utils.get_dataset_id()
        rdr_dataset_id = bq_utils.get_rdr_dataset_id()
        test_util.delete_all_tables(cls.bq_client, ehr_dataset_id)
        test_util.delete_all_tables(cls.bq_client, rdr_dataset_id)
        test_util.drop_hpo_id_bucket_name_table(cls.bq_client, cls.dataset_id)
