# -*- coding: utf-8 -*-
# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.7.1
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# + tags=["parameters"]
project_id = ""
old_rdr = ""
new_rdr = ""
raw_rdr = "default"  # do not need to provide this if running on a raw rdr import
new_rdr_sandbox = ""
run_as = ""
rdr_cutoff_date = ""
# -

# # QC for RDR Export
#
# Quality checks performed on a new RDR dataset and comparison with previous RDR dataset.
from common import JINJA_ENV, PIPELINE_TABLES
from utils import auth
from gcloud.bq import BigQueryClient
from analytics.cdr_ops.notebook_utils import execute, IMPERSONATION_SCOPES, render_message

# # Table comparison
# The export should generally contain the same tables from month to month.
# Tables found only in the old or the new export are listed below.

impersonation_creds = auth.get_impersonation_credentials(
    run_as, target_scopes=IMPERSONATION_SCOPES)

client = BigQueryClient(project_id, credentials=impersonation_creds)

tpl = JINJA_ENV.from_string('''
SELECT
  COALESCE(curr.table_id, prev.table_id) AS table_id
 ,curr.row_count AS _{{new_rdr}}
 ,prev.row_count AS _{{old_rdr}}
 ,(curr.row_count - prev.row_count) AS row_diff
FROM `{{project_id}}.{{new_rdr}}.__TABLES__` curr
FULL OUTER JOIN `{{project_id}}.{{old_rdr}}.__TABLES__` prev
  USING (table_id)
WHERE curr.table_id IS NULL OR prev.table_id IS NULL
''')
query = tpl.render(new_rdr=new_rdr, old_rdr=old_rdr, project_id=project_id)
execute(client, query)

# ## Row count comparison
# Generally the row count of clinical tables should increase from one export to the next.

tpl = JINJA_ENV.from_string('''
SELECT
  curr.table_id AS table_id
 ,prev.row_count AS _{{old_rdr}}
 ,curr.row_count AS _{{new_rdr}}
 ,(curr.row_count - prev.row_count) row_diff
FROM `{{project_id}}.{{new_rdr}}.__TABLES__` curr
JOIN `{{project_id}}.{{old_rdr}}.__TABLES__` prev
  USING (table_id)
ORDER BY ABS(curr.row_count - prev.row_count) DESC;
''')
query = tpl.render(new_rdr=new_rdr, old_rdr=old_rdr, project_id=project_id)
execute(client, query)

# ## ID range check
# Combine step may break if any row IDs in the RDR are larger than the added constant(1,000,000,000,000,000).
# Rows that are greater than 999,999,999,999,999 the will be listed out here.

domain_table_list = [
    'condition_occurrence', 'device_exposure', 'drug_exposure', 'location',
    'measurement', 'note', 'observation', 'procedure_occurrence', 'provider',
    'specimen', 'visit_occurrence'
]
queries = []
for table in domain_table_list:
    tpl = JINJA_ENV.from_string('''
    SELECT
        '{{table}}' AS domain_table_name,
        {{table}}_id AS domain_table_id
    FROM
     `{{project_id}}.{{new_rdr}}.{{table}}`
    WHERE
      {{table}}_id > 999999999999999
    ''')
    query = tpl.render(new_rdr=new_rdr, table=table, project_id=project_id)
    queries.append(query)
execute(client, '\nUNION ALL\n'.join(queries))

# ## Concept codes used
# Identify question and answer concept codes which were either added or removed
# (appear in only the new or only the old RDR datasets, respectively).

tpl = JINJA_ENV.from_string('''
WITH curr_code AS (
SELECT
  observation_source_value value
 ,'observation_source_value' field
 ,COUNT(1) row_count
FROM `{{project_id}}.{{new_rdr}}.observation` GROUP BY 1

UNION ALL

SELECT
  value_source_value value
 ,'value_source_value' field
 ,COUNT(1) row_count
FROM `{{project_id}}.{{new_rdr}}.observation` GROUP BY 1),

prev_code AS (
SELECT
  observation_source_value value
 ,'observation_source_value' field
 ,COUNT(1) row_count
FROM `{{project_id}}.{{old_rdr}}.observation` GROUP BY 1

UNION ALL

SELECT
  value_source_value value
 ,'value_source_value' field
 ,COUNT(1) row_count
FROM `{{project_id}}.{{old_rdr}}.observation` GROUP BY 1)

SELECT
  prev_code.value prev_code_value
 ,prev_code.field prev_code_field
 ,prev_code.row_count prev_code_row_count
 ,curr_code.value curr_code_value
 ,curr_code.field curr_code_field
 ,curr_code.row_count curr_code_row_count
FROM curr_code
 FULL OUTER JOIN prev_code
  USING (field, value)
WHERE prev_code.value IS NULL OR curr_code.value IS NULL
''')
query = tpl.render(new_rdr=new_rdr, old_rdr=old_rdr, project_id=project_id)
execute(client, query)

# # Question codes should have mapped `concept_id`s
# Question codes in `observation_source_value` should be associated with the concept identified by
# `observation_source_concept_id` and mapped to a standard concept identified by `observation_concept_id`.
# The table below lists codes having rows where either field is null or zero and the number of rows where this occurs.
# This may be associated with an issue in the PPI vocabulary or in the RDR ETL process.
#
# Note: Snap codes are not modeled in the vocabulary but may be used in the RDR export.
# They are excluded here by filtering out snap codes in the Public PPI Codebook
# which were loaded into `curation_sandbox.snap_codes`.

tpl = JINJA_ENV.from_string("""
SELECT
  observation_source_value
 ,COUNTIF(observation_source_concept_id IS NULL) AS source_concept_id_null
 ,COUNTIF(observation_source_concept_id=0)       AS source_concept_id_zero
 ,COUNTIF(observation_concept_id IS NULL)        AS concept_id_null
 ,COUNTIF(observation_concept_id=0)              AS concept_id_zero
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE observation_source_value IS NOT NULL
AND observation_source_value != ''
AND observation_source_value NOT IN (SELECT concept_code FROM `{{project_id}}.curation_sandbox.snap_codes`)
GROUP BY 1
HAVING source_concept_id_null + source_concept_id_zero + concept_id_null + concept_id_zero > 0
ORDER BY 2 DESC, 3 DESC, 4 DESC, 5 DESC
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Answer codes should have mapped `concept_id`s
# Answer codes in value_source_value should be associated with the concept identified by value_source_concept_id
# and mapped to a standard concept identified by value_as_concept_id. The table below lists codes having rows
# where either field is null or zero and the number of rows where this occurs.
# This may be associated with an issue in the PPI vocabulary or in the RDR ETL process.
#
# Note: Snap codes are not modeled in the vocabulary but may be used in the RDR export.
# They are excluded here by filtering out snap codes in the Public PPI Codebook
# which were loaded into `curation_sandbox.snap_codes`.
#

tpl = JINJA_ENV.from_string("""
SELECT
  value_source_value
 ,COUNTIF(value_source_concept_id IS NULL) AS source_concept_id_null
 ,COUNTIF(value_source_concept_id=0)       AS source_concept_id_zero
 ,COUNTIF(value_as_concept_id IS NULL)     AS concept_id_null
 ,COUNTIF(value_as_concept_id=0)           AS concept_id_zero
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE value_source_value IS NOT NULL
AND value_source_value != ''
AND value_source_value NOT IN (SELECT concept_code FROM `{{project_id}}.curation_sandbox.snap_codes`)
GROUP BY 1
HAVING source_concept_id_null + source_concept_id_zero + concept_id_null + concept_id_zero > 0
ORDER BY 2 DESC, 3 DESC, 4 DESC, 5 DESC
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Dates are equal in observation_date and observation_datetime
# Any mismatches are listed below.

tpl = JINJA_ENV.from_string("""
SELECT
  observation_id
 ,person_id
 ,observation_date
 ,observation_datetime
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE observation_date != EXTRACT(DATE FROM observation_datetime)
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check for duplicates

tpl = JINJA_ENV.from_string("""
with duplicates AS (
    SELECT
      person_id
     ,observation_datetime
     ,observation_source_value
     ,value_source_value
     ,value_as_number
     ,value_as_string
   -- ,questionnaire_response_id --
     ,COUNT(1) AS n_data
    FROM `{{project_id}}.{{new_rdr}}.observation`
    INNER JOIN `{{project_id}}.{{new_rdr}}.cope_survey_semantic_version_map` 
        USING (questionnaire_response_id) -- For COPE only --
    GROUP BY 1,2,3,4,5,6
)
SELECT
  n_data   AS duplicates
 ,COUNT(1) AS n_duplicates
FROM duplicates
WHERE n_data > 1
GROUP BY 1
ORDER BY 2 DESC
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check if numeric data in value_as_string
# Some numeric data is expected in value_as_string.  For example, zip codes or other contact specific information.

tpl = JINJA_ENV.from_string("""
SELECT
  observation_source_value
 ,COUNT(1) AS n
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE SAFE_CAST(value_as_string AS INT64) IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # All COPE `questionnaire_response_id`s are in COPE version map
# Any `questionnaire_response_id`s missing from the map will be listed below.

tpl = JINJA_ENV.from_string("""
SELECT
  observation_id
 ,person_id
 ,questionnaire_response_id
FROM `{{project_id}}.{{new_rdr}}.observation`
 INNER JOIN `{{project_id}}.pipeline_tables.cope_concepts`
  ON observation_source_value = concept_code
WHERE questionnaire_response_id NOT IN
(SELECT questionnaire_response_id FROM `{{project_id}}.{{new_rdr}}.cope_survey_semantic_version_map`)
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # No duplicate `questionnaire_response_id`s in COPE version map
# Any duplicated `questionnaire_response_id`s will be listed below.

tpl = JINJA_ENV.from_string("""
SELECT
  questionnaire_response_id
 ,COUNT(*) n
FROM `{{project_id}}.{{new_rdr}}.cope_survey_semantic_version_map`
GROUP BY questionnaire_response_id
HAVING n > 1
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Survey version and dates
# This query checks the validity of cope_survey_semantic_version_map table which contains the version of each
# COPE and/or Minute module that each participant took.
# This table is created by RDR and is included in the rdr export. <br>
# For each COPE and Minute module the min and max survey observation dates in the RDR are listed,
# as well as a count of surveys taken outside of each module's expected implementation range. <br>
# Expected implementation ranges are found in the query or in
# [this documentation](https://docs.google.com/document/d/1IhRnvAymSZeko8AbS4TCaqnw_78Qa_NkTROfHOFqGGQ/edit?usp=sharing)
#  - If all surveys have data(10 modules), and the *_failure columns have a result = 0 this check PASSES.
#  - If all 10 surveys are not represented in the query results, this check FAILS.
#  Notify RDR of the missing survey data.
#  - If any of the *_failure columns have a result > 0 this check FAILS.
#  Notify RDR that there are surveys with observation_dates outside of the survey's expected implementation range.
#

tpl = JINJA_ENV.from_string("""
SELECT
 cope_month AS survey_version
,MIN(observation_date) AS min_obs_date
,MAX(observation_date) AS max_obs_date
,COUNTIF(cope_month ='may' AND observation_date NOT BETWEEN '2020-05-07' AND '2020-05-30' ) AS may_failure
,COUNTIF(cope_month ='june' AND observation_date NOT BETWEEN '2020-06-02' AND '2020-06-26' ) AS june_failure
,COUNTIF(cope_month ='july' AND observation_date NOT BETWEEN '2020-07-07' AND '2020-09-25' ) AS july_failure
,COUNTIF(cope_month ='nov' AND observation_date NOT BETWEEN '2020-10-27' AND '2020-12-03' ) AS nov_failure
,COUNTIF(cope_month ='dec' AND observation_date NOT BETWEEN '2020-12-08' AND '2021-01-04' ) AS dec_failure
,COUNTIF(cope_month ='feb' AND observation_date NOT BETWEEN '2021-02-08' AND '2021-03-05' ) AS feb_failure
,COUNTIF(cope_month ='vaccine1' AND observation_date NOT BETWEEN '2021-06-10' AND '2021-08-19' ) AS summer_failure
,COUNTIF(cope_month ='vaccine2' AND observation_date NOT BETWEEN '2021-08-19' AND '2021-10-28' ) AS fall_failure
,COUNTIF(cope_month ='vaccine3' AND observation_date NOT BETWEEN '2021-10-28' AND '2022-01-20' ) AS winter_failure
,COUNTIF(cope_month ='vaccine4' AND observation_date NOT BETWEEN '2022-01-20' AND '2022-03-08' ) AS new_year_failure
FROM `{{project_id}}.{{new_rdr}}.observation`
JOIN `{{project_id}}.{{new_rdr}}.cope_survey_semantic_version_map` USING (questionnaire_response_id)
GROUP BY 1
ORDER BY MIN(observation_date)
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Class of PPI Concepts using vocabulary.py
# Concept codes which appear in `observation.observation_source_value` should belong to concept class Question.
# Concept codes which appear in `observation.value_source_value` should belong to concept class Answer.
# Concepts of class Qualifier Value are permitted as a value and
# Concepts of class Topic and PPI Modifier are permitted as a question
# Discreprancies (listed below) can be caused by misclassified entries in Athena or
# invalid payloads in the RDR and in further upstream data sources.

tpl = JINJA_ENV.from_string('''
WITH ppi_concept_code AS (
 SELECT
   observation_source_value AS code
  ,'Question'               AS expected_concept_class_id
  ,COUNT(1) n
 FROM `{{project_id}}.{{new_rdr}}.observation`
 GROUP BY 1, 2

 UNION ALL

 SELECT DISTINCT
   value_source_value AS code
  ,'Answer'           AS expected_concept_class_id
  ,COUNT(1) n
 FROM `{{project_id}}.{{new_rdr}}.observation`
 GROUP BY 1, 2
)
SELECT
  code
 ,expected_concept_class_id
 ,concept_class_id
 ,n
FROM ppi_concept_code
JOIN `{{project_id}}.{{new_rdr}}.concept`
 ON LOWER(concept_code)=LOWER(code)
WHERE LOWER(concept_class_id)<>LOWER(expected_concept_class_id)
AND CASE WHEN expected_concept_class_id = 'Question' THEN concept_class_id NOT IN('Topic','PPI Modifier') END
AND concept_class_id != 'Qualifier Value'
ORDER BY 1, 2, 3
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Identify Questions That Dont Exist in the RDR Export
# This identifies questions as indicated by a PPI vocabulary and Question concept_class_id that
# do not exist in the dataset.

tpl = JINJA_ENV.from_string("""
with question_codes as (select c.concept_id, c.concept_name, c.concept_class_id
from `{{project_id}}.{{new_rdr}}.concept` as c
where REGEXP_CONTAINS(c.vocabulary_id, r'(?i)(ppi)') and REGEXP_CONTAINS(c.concept_class_id, r'(?i)(question)'))
, used_q_codes as (
    select distinct o.observation_source_concept_id, o.observation_source_value
    from `{{project_id}}.{{new_rdr}}.observation` as o
    join `{{project_id}}.{{new_rdr}}.concept` as c
    on o.observation_source_concept_id = c.concept_id
    where REGEXP_CONTAINS(c.vocabulary_id, r'(?i)(ppi)') and REGEXP_CONTAINS(c.concept_class_id, r'(?i)(question)')
)
    SELECT *
    from question_codes
    where concept_id not in (select observation_source_concept_id from used_q_codes)
    """)
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Identify Questions That Dont Exist in the Cleaned RDR Export
# This identifies questions as indicated by a PPI vocabulary and Question concept_class_id that
# do not exist in the cleaned dataset but did exist in the raw dataset.

if raw_rdr == "default":
    print("Not running code to validate cleaned dataset against raw dataset.")
else:
    tpl = JINJA_ENV.from_string("""
    with question_codes as (select c.concept_id, c.concept_name, c.concept_class_id
    from `{{project_id}}.{{new_rdr}}.concept` as c
    where REGEXP_CONTAINS(c.vocabulary_id, r'(?i)(ppi)') and REGEXP_CONTAINS(c.concept_class_id, r'(?i)(question)'))
    , used_q_codes as (
        select distinct o.observation_source_concept_id, o.observation_source_value
        from `{{project_id}}.{{new_rdr}}.observation` as o
        join `{{project_id}}.{{new_rdr}}.concept` as c
        on o.observation_source_concept_id = c.concept_id
        where REGEXP_CONTAINS(c.vocabulary_id, r'(?i)(ppi)') and REGEXP_CONTAINS(c.concept_class_id, r'(?i)(question)')
    ), used_rawq_codes as (
        select distinct o.observation_source_concept_id, o.observation_source_value
        from `{{project_id}}.{{raw_rdr}}.observation` as o
        join `{{project_id}}.{{raw_rdr}}.concept` as c
        on o.observation_source_concept_id = c.concept_id
        where REGEXP_CONTAINS(c.vocabulary_id, r'(?i)(ppi)') and REGEXP_CONTAINS(c.concept_class_id, r'(?i)(question)')
    )
        SELECT *
        from question_codes
        where concept_id not in (select observation_source_concept_id from used_q_codes)
        and concept_id in (select observation_source_concept_id from used_rawq_codes)
        """)
    query = tpl.render(new_rdr=new_rdr, project_id=project_id, raw_rdr=raw_rdr)
    execute(client, query)

# # Make sure previously corrected missing data still exists
# Make sure that the cleaning rule clash that previously wiped out all numeric smoking data is corrected.
# Any returned rows indicate a problem that needs to be fixed.  Identified rows when running on a raw rdr
# import indicates a problem with the RDR ETL and will require cross team coordination.  Identified rows
# when running on a cleaned rdr import indicate problems with cleaning rules that should be remediated by curation.
#
# Make sure the Sexuality Closer Description (observation_source_concept_id = 1585357) rows still exist
# Curation has lost this data due to bad ppi branching logic.  This check is to ensure we do
# not lose this particular data again.  If rows are identified, then there is an issue with the cleaning
# rules (possibly PPI branching) that must be resolved.  This has resulted in a previous hotfix.
# We do not want to repeat the hotfix process.

tpl = JINJA_ENV.from_string('''
SELECT
    observation_source_concept_id
    ,observation_source_value
    ,value_source_concept_id
    ,value_source_value
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE
  -- check for smoking answers --
  ((observation_source_concept_id IN (1585864, 1585870,1585873, 1586159, 1586162)
    AND value_as_number IS NOT NULL)
    -- check for sexuality answers --
  OR (observation_source_concept_id in (1585357)))
GROUP BY 1, 2, 3, 4
HAVING count(*) = 0
ORDER BY 1, 3
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# ## Participants must have basics data
# Identify any participants who have don't have any responses
# to questions in the basics survey module (see [DC-706](https://precisionmedicineinitiative.atlassian.net/browse/DC-706)). These should be
# reported to the RDR as they are supposed to be filtered out
# from the RDR export.

# +
BASICS_MODULE_CONCEPT_ID = 1586134

# Note: This assumes that concept_ancestor sufficiently
# represents the hierarchy
tpl = JINJA_ENV.from_string("""
WITH 

 -- all PPI question concepts in the basics survey module --
 basics_concept AS
 (SELECT
   c.concept_id
  ,c.concept_name
  ,c.concept_code
  FROM `{{DATASET_ID}}.concept_ancestor` ca
  JOIN `{{DATASET_ID}}.concept` c
   ON ca.descendant_concept_id = c.concept_id
  WHERE 1=1
    AND ancestor_concept_id={{BASICS_MODULE_CONCEPT_ID}}
    AND c.vocabulary_id='PPI'
    AND c.concept_class_id='Question')

 -- maps pids to all their associated basics questions in the rdr --
,pid_basics AS
 (SELECT
   person_id 
  ,ARRAY_AGG(DISTINCT c.concept_code IGNORE NULLS) basics_codes
  FROM `{{DATASET_ID}}.observation` o
  JOIN basics_concept c
   ON o.observation_concept_id = c.concept_id
  WHERE 1=1
  GROUP BY 1)

 -- list all pids for whom no basics questions are found --
SELECT * 
FROM `{{DATASET_ID}}.person`
WHERE person_id not in (select person_id from pid_basics)
""")
query = tpl.render(DATASET_ID=new_rdr,
                   BASICS_MODULE_CONCEPT_ID=BASICS_MODULE_CONCEPT_ID)
execute(client, query)
# -

# # Date conformance check
# COPE surveys contain some concepts that must enforce dates in the observation.value_as_string field.
# For the observation_source_concept_id = 715711, if the value in value_as_string does not meet a standard date format
# of YYYY-mm-dd, return a dataframe with the observation_id and person_id
# Curation needs to contact the RDR team about data discrepancies

tpl = JINJA_ENV.from_string('''
SELECT
    observation_id
    ,person_id
    ,value_as_string
FROM `{{project_id}}.{{new_rdr}}.observation` 
WHERE observation_source_concept_id = 715711
AND SAFE_CAST(value_as_string AS DATE) IS NULL 
AND value_as_string != 'PMI Skip'
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check pid_rid_mapping table for duplicates
# Duplicates are not allowed in the person_id or research_id columns of the pid_rid_mapping table.
# If found, there is a problem with the RDR import. An RDR On-Call ticket should be opened
# to report the problem. In ideal circumstances, this query will not return any results.
# If a result set is returned, an error has been found for the identified field.
# If the table was not imported, the filename changed, or field names changed,
# this query will fail by design to indicate an unexpected change has occurred.

tpl = JINJA_ENV.from_string('''
SELECT
    'person_id' as id_type
    ,person_id as id
    ,COUNT(person_id) as count
FROM `{{project_id}}.{{new_rdr}}.pid_rid_mapping`
GROUP BY person_id
HAVING count > 1

UNION ALL

SELECT
    'research_id' as id_type
    ,research_id as id
    ,COUNT(research_id) as count
FROM `{{project_id}}.{{new_rdr}}.pid_rid_mapping`
GROUP BY research_id
HAVING count > 1
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Ensure all person_ids exist in the person table and have mappings
# All person_ids in the pid_rid_mapping table should exist in the person table.
# If the person record does not exist for a mapping record, there is a problem with the RDR import.
# An RDR On-Call ticket should be opened to report the problem.
# All person_ids in the person table should have a mapping in the pid_rid_mapping table.
# If any person_ids do not have a mapping record, there is a problem with the RDR import.
# An RDR On-Call ticket should be opened to report the problem.
# In ideal circumstances, this query will not return any results.

tpl = JINJA_ENV.from_string('''
SELECT
    'missing_person' as issue_type
    ,person_id
FROM `{{project_id}}.{{new_rdr}}.pid_rid_mapping`
WHERE person_id NOT IN 
(SELECT person_id
FROM `{{project_id}}.{{new_rdr}}.person`)

UNION ALL

SELECT 
    'unmapped_person' as issue_type
    ,person_id
FROM `{{project_id}}.{{new_rdr}}.person`
WHERE person_id NOT IN 
(SELECT person_id
FROM `{{project_id}}.{{new_rdr}}.pid_rid_mapping`)
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check for inconsistencies between primary and RDR pid_rid_mappings
# Mappings which were in previous exports may be removed from a new export for two reasons:
#   1. Participants have withdrawn or
#   2. They were identified as test or dummy data
# Missing mappings from the previous RDR export are therefore not a significant cause for concern.
# However, mappings in the RDR pid_rid_mapping should always be consistent with the
# primary_pid_rid_mapping in pipeline_tables for existing mappings.
# If the same pid has different rids in the pid_rid_mapping and the primary_pid_rid_mapping,
# there is a problem with the RDR import. An RDR On-Call ticket should be opened to report the problem.
# In ideal circumstances, this query will not return any results.

tpl = JINJA_ENV.from_string('''
SELECT
    person_id
FROM `{{project_id}}.{{new_rdr}}.pid_rid_mapping` rdr
JOIN `{{project_id}}.pipeline_tables.primary_pid_rid_mapping` primary
USING (person_id)
WHERE primary.research_id <> rdr.research_id
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Checks for basics survey module
# Participants with data in other survey modules must also have data from the basics survey module.
# This check identifies survey responses associated with participants that do not have any responses
# associated with the basics survey module.
# In ideal circumstances, this query will not return any results.

tpl = JINJA_ENV.from_string('''
SELECT DISTINCT person_id FROM `{{project_id}}.{{new_rdr}}.observation` 
JOIN `{{project_id}}.{{new_rdr}}.concept` on (observation_source_concept_id=concept_id)
WHERE vocabulary_id = 'PPI' AND person_id NOT IN (
SELECT DISTINCT person_id FROM `{{project_id}}.{{new_rdr}}.concept` 
JOIN `{{project_id}}.{{new_rdr}}.concept_ancestor` on (concept_id=ancestor_concept_id)
JOIN `{{project_id}}.{{new_rdr}}.observation` on (descendant_concept_id=observation_concept_id)
WHERE concept_class_id='Module'
AND concept_name IN ('The Basics')
AND questionnaire_response_id IS NOT NULL)
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# ## Participants must be 18 years of age or older to consent
#
# AOU participants are required to be 18+ years of age at the time of consent
# ([DC-1724](https://precisionmedicineinitiative.atlassian.net/browse/DC-1724)),
# based on the date associated with the [ExtraConsent_TodaysDate](https://athena.ohdsi.org/search-terms/terms/1585482)
# row. Any violations should be reported to the RDR team as these should have been filtered out by the RDR ETL process
# ([DA-2073](https://precisionmedicineinitiative.atlassian.net/browse/DA-2073)).

tpl = JINJA_ENV.from_string('''
SELECT *
FROM `{{project_id}}.{{new_rdr}}.observation`
JOIN `{{project_id}}.{{new_rdr}}.person` USING (person_id)
WHERE  (observation_source_concept_id=1585482 OR observation_concept_id=1585482)
AND {{PIPELINE_TABLES}}.calculate_age(observation_date, EXTRACT(DATE FROM birth_datetime)) < 18
''')
query = tpl.render(new_rdr=new_rdr,
                   project_id=project_id,
                   PIPELINE_TABLES=PIPELINE_TABLES)
execute(client, query)

# # Check for missing questionnaire_response_id

# Survey data in the RDR export should all have **questionnaire_response_id**
# except the pmi skip data backfilled by Curation cleaning rule.
# Any violations should be reported to the RDR team.
# [DC-1776](https://precisionmedicineinitiative.atlassian.net/browse/DC-1776).
# [DC-2254](https://precisionmedicineinitiative.atlassian.net/browse/DC-2254).

tpl = JINJA_ENV.from_string('''
SELECT 
    person_id, 
    STRING_AGG(observation_source_value) AS observation_source_value
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE observation_type_concept_id = 45905771 -- is a survey response --
AND NOT (observation_id >= 1000000000000 AND value_as_concept_id = 903096) -- exclude records from backfill pmi skip --
AND questionnaire_response_id IS NULL
GROUP BY 1
''')
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check if concepts for operational use still exist in the data

# According to [this ticket](https://precisionmedicineinitiative.atlassian.net/browse/DC-1792),
# the RDR export should not contain some operational concepts that are irrelevant to researchers.
# Any violations should be reported to the RDR team.

tpl = JINJA_ENV.from_string("""
SELECT 
    observation_source_value,
    COUNT(1) AS n_row_violation
FROM `{{project_id}}.{{new_rdr}}.observation`
WHERE observation_source_value IN (
  SELECT observation_source_value FROM `{{project_id}}.operational_data.operational_ehr_consent`
)
GROUP BY 1
HAVING count(1) > 0
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check if Responses for question [46234786](https://athena.ohdsi.org/search-terms/terms/46234786)
# # are updated to 2000000010 - AoUDRC_ResponseRemoval from dates ranging 11/1/2021 – 11/9/2021

# According to [this ticket](https://precisionmedicineinitiative.atlassian.net/browse/DC-2118),
# the RDR export should not contain any responses other than 2000000010 - AoUDRC_ResponseRemoval for
# question - [46234786](https://athena.ohdsi.org/search-terms/terms/46234786) ranging from dates 11/1/2021 – 11/9/2021
# this check will give count of responses that does not meet this condition. Having ) count means this check is passed.

tpl = JINJA_ENV.from_string("""
SELECT
    value_source_concept_id, value_as_concept_id, count(*) as n_row_violation
FROM
 `{{project_id}}.{{new_rdr}}.observation`
WHERE
  observation_source_concept_id = 46234786
  AND (observation_date >= DATE('2021-11-01')
    AND observation_date <= DATE('2021-11-09'))
  AND (value_as_concept_id <> 2000000010
    OR value_source_concept_id <> 2000000010)
group by value_source_concept_id, value_as_concept_id
""")
query = tpl.render(new_rdr=new_rdr, project_id=project_id)
execute(client, query)

# # Check that the Question and Answer Concepts in the old_map_short_codes tables are not paired with 0-valued concept_identifiers

# According to this [ticket](https://precisionmedicineinitiative.atlassian.net/browse/DC-2488), Question and Answer concepts that are identified in the `old_map_short_codes` table should not be paired with 0-valued concept_identifiers after the RDR dataset is cleaned. These concept identifiers include the `observation_concept_id` and `observation_source_concept_id` fields.

# ## Question Codes

# Check the question codes
tpl = JINJA_ENV.from_string("""
WITH question_codes AS (
  SELECT
    pmi_code
  FROM `{{project_id}}.{{sandbox_dataset}}.old_map_short_codes` 
  WHERE type = 'Question'
)
SELECT
  qc.pmi_code, o.observation_source_value, o.observation_concept_id, o.observation_source_concept_id, COUNT(*) invalid_id_count
FROM `{{project_id}}.{{dataset}}.observation` o
JOIN question_codes qc
  ON qc.pmi_code = o.observation_source_value
WHERE (o.observation_source_concept_id = 0
  OR o.observation_concept_id = 0)
GROUP BY qc.pmi_code, o.observation_source_value, o.observation_concept_id, o.observation_source_concept_id
ORDER BY invalid_id_count DESC
""")
query = tpl.render(project_id=project_id,
                   dataset=new_rdr,
                   sandbox_dataset=new_rdr_sandbox)
df = execute(client, query)

# +
success_msg = 'No 0-valued concept ids found.'
failure_msg = '''
    <b>{code_count}</b> question codes have 0-valued IDs. Report failure back to curation team.
    Bug likely due to failure in the <code>update_questions_answers_not_mapped_to_omop</code> cleaning rule.
'''

render_message(df,
               success_msg,
               failure_msg,
               failure_msg_args={'code_count': len(df)})
# -

# ## Answer Codes

# Check the answer codes
tpl = JINJA_ENV.from_string("""
WITH answer_codes AS (
  SELECT
    pmi_code
  FROM `{{project_id}}.{{sandbox_dataset}}.old_map_short_codes` 
  WHERE type = 'Answer'
)
SELECT
  ac.pmi_code, o.value_source_value, o.value_source_concept_id, o.value_as_concept_id, COUNT(*) invalid_id_count
FROM `{{project_id}}.{{dataset}}.observation` o
JOIN answer_codes ac
  ON ac.pmi_code = o.value_source_value
WHERE (o.value_source_concept_id = 0
  OR o.value_as_concept_id = 0)
GROUP BY ac.pmi_code, o.value_source_value, o.value_source_concept_id, o.value_as_concept_id
ORDER BY invalid_id_count DESC
""")
query = tpl.render(project_id=project_id,
                   dataset=new_rdr,
                   sandbox_dataset=new_rdr_sandbox)
df = execute(client, query)

# +
success_msg = 'No 0-valued concept ids found.'
failure_msg = '''
    <b>{code_count}</b> answer codes have 0-valued IDs. Report failure back to curation team.
    Bug likely due to failure in the <code>update_questions_answers_not_mapped_to_omop</code> cleaning rule.
'''

render_message(df,
               success_msg,
               failure_msg,
               failure_msg_args={'code_count': len(df)})
# -

# ### Question-Answer Codes Combo

# Check that mapped answer codes are paired with correctly mapped question codes.  If the question codes are zero valued, the question and answer pair will be dropped from the clean version of the CDR.
tpl = JINJA_ENV.from_string("""
WITH answer_codes AS (
  SELECT
    pmi_code
  FROM `{{project_id}}.{{sandbox_dataset}}.old_map_short_codes` 
  WHERE type = 'Answer'
)
SELECT
  ac.pmi_code, o.value_source_value, o.value_source_concept_id, o.value_as_concept_id,
  o.observation_source_value, o.observation_concept_id, o.observation_source_concept_id, COUNT(*) invalid_id_count
FROM `{{project_id}}.{{dataset}}.observation` o
JOIN answer_codes ac
  ON ac.pmi_code = o.value_source_value
WHERE (o.observation_source_concept_id = 0
  OR o.observation_concept_id = 0)
GROUP BY ac.pmi_code, o.value_source_value, o.value_source_concept_id, o.value_as_concept_id,
  o.observation_source_value, o.observation_concept_id, o.observation_source_concept_id
ORDER BY invalid_id_count DESC
""")
query = tpl.render(project_id=project_id,
                   dataset=new_rdr,
                   sandbox_dataset=new_rdr_sandbox)
df = execute(client, query)

# +
success_msg = 'No 0-valued concept ids found.'
failure_msg = '''
    <b>{code_count}</b> question codes have 0-valued IDs (<em>answer codes visible</em>). Report failure back to curation team.
    Bug likely due to failure in the <code>update_questions_answers_not_mapped_to_omop</code> cleaning rule.
'''

render_message(df,
               success_msg,
               failure_msg,
               failure_msg_args={'code_count': len(df)})

# -

# # COPE survey mapping

# There is a known issue that COPE survey questions all map to the module
# 1333342 (COPE survey with no version specified). This check is to confirm
# if this issue still exists in the vocabulary or not.
# If this issue is fixed, each COPE survey questions will have mapping to
# individual COPE survey modules and will no longer have mapping to 1333342.
# cope_question_concept_ids are collected using the SQL listed in DC-2641:
# [DC-2641](https://precisionmedicineinitiative.atlassian.net/browse/DC-2641).

cope_question_concept_ids = [
    596884, 596885, 596886, 596887, 596888, 702686, 713888, 715711, 715713,
    715714, 715719, 715720, 715721, 715722, 715723, 715724, 715725, 715726,
    903629, 903630, 903631, 903632, 903633, 903634, 903635, 903641, 903642,
    1310051, 1310052, 1310053, 1310054, 1310056, 1310058, 1310060, 1310062,
    1310065, 1310066, 1310067, 1310132, 1310133, 1310134, 1310135, 1310136,
    1310137, 1310138, 1310139, 1310140, 1310141, 1310142, 1310144, 1310145,
    1310146, 1310147, 1310148, 1332734, 1332735, 1332737, 1332738, 1332739,
    1332741, 1332742, 1332744, 1332745, 1332746, 1332747, 1332748, 1332749,
    1332750, 1332751, 1332752, 1332753, 1332754, 1332755, 1332756, 1332762,
    1332763, 1332767, 1332769, 1332792, 1332793, 1332794, 1332795, 1332796,
    1332797, 1332800, 1332801, 1332802, 1332803, 1332804, 1332805, 1332806,
    1332807, 1332808, 1332819, 1332820, 1332822, 1332824, 1332826, 1332828,
    1332829, 1332830, 1332831, 1332832, 1332833, 1332835, 1332843, 1332847,
    1332848, 1332849, 1332853, 1332854, 1332861, 1332862, 1332863, 1332866,
    1332867, 1332868, 1332869, 1332870, 1332871, 1332872, 1332874, 1332876,
    1332878, 1332880, 1332935, 1332937, 1332944, 1332998, 1333004, 1333011,
    1333012, 1333013, 1333014, 1333015, 1333016, 1333017, 1333018, 1333019,
    1333020, 1333021, 1333022, 1333023, 1333024, 1333102, 1333104, 1333105,
    1333118, 1333119, 1333120, 1333121, 1333156, 1333163, 1333164, 1333165,
    1333166, 1333167, 1333168, 1333182, 1333183, 1333184, 1333185, 1333186,
    1333187, 1333188, 1333189, 1333190, 1333191, 1333192, 1333193, 1333194,
    1333195, 1333200, 1333216, 1333221, 1333234, 1333235, 1333274, 1333275,
    1333276, 1333277, 1333278, 1333279, 1333280, 1333281, 1333285, 1333286,
    1333287, 1333288, 1333289, 1333291, 1333292, 1333293, 1333294, 1333295,
    1333296, 1333297, 1333298, 1333299, 1333300, 1333301, 1333303, 1333311,
    1333312, 1333313, 1333314, 1333324, 1333325, 1333326, 1333327, 1333328
]

tpl = JINJA_ENV.from_string("""
WITH question_topic_module AS (
  SELECT
      cr1.concept_id_1 AS question, 
      cr1.concept_id_2 AS topic, 
      cr2.concept_id_2 AS module
  FROM `{{projcet_id}}.{{dataset}}.concept_relationship` cr1
  JOIN `{{projcet_id}}.{{dataset}}.concept` c1 ON cr1.concept_id_2 = c1.concept_id
  JOIN `{{projcet_id}}.{{dataset}}.concept_relationship` cr2 ON c1.concept_id = cr2.concept_id_1
  JOIN `{{projcet_id}}.{{dataset}}.concept` c2 ON cr2.concept_id_2 = c2.concept_id
  WHERE cr1.concept_id_1 IN ({{cope_question_concept_ids}})
  AND c1.concept_class_id = 'Topic'
  AND c2.concept_class_id = 'Module'
)
SELECT DISTINCT question FROM question_topic_module
WHERE module = 1333342
""")
query = tpl.render(
    new_rdr=new_rdr,
    project_id=project_id,
    dataset=new_rdr,
    cope_question_concept_ids=", ".join(
        str(concept_id) for concept_id in cope_question_concept_ids))
df = execute(client, query)

# +
success_msg = '''
    The mapping issue is resolved. Double-check each concept is mapped to individual COPE module.
    Once we double-checked it, we can remove this QC from this notebook.
'''
failure_msg = '''
    The mapping issue still exists. There are <b>{code_count}</b> concepts for COPE questions
    that map to 1333342. Notify Odysseus that the issue still persists.
    For pipeline, we can use cope_survey_semantic_version_map to diffrentiate COPE module versions,
    so we can still move on. See DC-2641 for detail.
'''

render_message(df,
               success_msg,
               failure_msg,
               failure_msg_args={'code_count': len(df)})
# -

# ### RDR date cutoff check

# Check that survey dates are not beyond the RDR cutoff date, also check observation.
query = JINJA_ENV.from_string("""
SELECT
  'observation' AS TABLE,
  COUNT(*) AS rows_beyond_cutoff
FROM
  `{{project_id}}.{{new_rdr}}.observation`
WHERE
  observation_date > DATE('{{rdr_cutoff_date}}')
UNION ALL
SELECT
  'survey_conduct_start' AS TABLE,
  COUNT(*) AS rows_beyond_cutoff
FROM
  `{{project_id}}.{{new_rdr}}.survey_conduct`
WHERE
  survey_start_date > DATE('{{rdr_cutoff_date}}')
UNION ALL
SELECT
  'survey_conduct_end' AS TABLE,
  COUNT(*) AS rows_beyond_cutoff
FROM
  `{{project_id}}.{{new_rdr}}.survey_conduct`
WHERE
  survey_end_date > DATE('{{rdr_cutoff_date}}')
""").render(project_id=project_id,
            new_rdr=new_rdr,
            rdr_cutoff_date=rdr_cutoff_date)

execute(client, query)
