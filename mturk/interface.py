import datetime

from boto.mturk.connection import MTurkConnection, MTurkRequestError
from boto.mturk.price import Price
from boto.mturk.qualification import (LocaleRequirement,
                                      NumberHitsApprovedRequirement,
                                      PercentAssignmentsApprovedRequirement,
                                      Qualifications)
from boto.mturk.question import ExternalQuestion
from django.db.models import Q
from hashids import Hashids

from crowdsourcing.models import Task, TaskWorker
from csp import settings
from mturk.models import MTurkHIT


class MTurkProvider(object):
    description = 'This is a task authored by a requester on Daemo, a research crowdsourcing platform. ' \
                  'Mechanical Turk workers are welcome to do it'
    keywords = ['daemo']
    countries = ['US', 'CA']
    min_hits = 1000

    def __init__(self, host, aws_access_key_id, aws_secret_access_key):
        self.host = host
        self.connection = MTurkConnection(aws_access_key_id=aws_access_key_id,
                                          aws_secret_access_key=aws_secret_access_key, host=settings.MTURK_HOST)
        self.connection.APIVersion = "2014-08-15"
        if not self.host:
            raise ValueError("Please provide a host url")

    def get_connection(self):
        return self.connection

    def get_qualifications(self):
        requirements = []
        approved_hits = NumberHitsApprovedRequirement('GreaterThan', self.min_hits)
        percentage_approved = PercentAssignmentsApprovedRequirement('GreaterThanOrEqualTo', 97)
        locale = MultiLocaleRequirement('In', self.countries)
        requirements.append(locale)
        requirements.append(approved_hits)
        requirements.append(percentage_approved)
        return Qualifications(requirements)

    def create_hits(self, project, tasks=None, repetition=None):
        if project.min_rating > 0:
            return 'NOOP'
        title = project.name
        reward = Price(project.price)
        if not tasks:
            query = '''
                SELECT t.id, count(tw.id) worker_count FROM
                crowdsourcing_task t
                LEFT OUTER JOIN crowdsourcing_taskworker tw ON t.id = tw.task_id AND tw.task_status
                NOT IN (%(skipped)s, %(rejected)s)
                WHERE project_id = %(project_id)s
                GROUP BY t.id
            '''
            tasks = Task.objects.raw(query, params={'skipped': TaskWorker.STATUS_SKIPPED,
                                                    'rejected': TaskWorker.STATUS_REJECTED, 'project_id': project.id})
        for task in tasks:
            question = self.create_external_question(task)
            if str(settings.MTURK_ONLY) == 'True':
                max_assignments = project.repetition
            else:
                max_assignments = 1
            qualifications = None
            if str(settings.MTURK_QUALIFICATIONS) == 'True':
                qualifications = self.get_qualifications()
            if not MTurkHIT.objects.filter(task=task):
                hit = self.connection.create_hit(hit_type=None, max_assignments=max_assignments,
                                                 title=title, reward=reward,
                                                 duration=datetime.timedelta(hours=settings.MTURK_COMPLETION_TIME),
                                                 description=self.description, keywords=self.keywords,
                                                 qualifications=qualifications,
                                                 approval_delay=datetime.timedelta(days=2),
                                                 question=question)[0]
                self.set_notification(hit_type_id=hit.HITTypeId)
                mturk_hit = MTurkHIT(hit_id=hit.HITId, hit_type_id=hit.HITTypeId, task=task)
                mturk_hit.save()
        return 'SUCCESS'

    def create_external_question(self, task, frame_height=800):
        task_hash = Hashids(salt=settings.SECRET_KEY, min_length=settings.ID_HASH_MIN_LENGTH)
        task_id = task_hash.encode(task.id)
        url = self.host + '/mturk/task/?taskId=' + task_id
        question = ExternalQuestion(external_url=url, frame_height=frame_height)
        return question

    def update_max_assignments(self, task):
        task = Task.objects.get(id=task['id'])
        mturk_hit = task.mturk_hit
        if not mturk_hit:
            raise MTurkHIT.DoesNotExist("This task is not associated to any mturk hit")
        assignments_completed = task.task_workers.filter(~Q(task_status__in=[TaskWorker.STATUS_REJECTED,
                                                                             TaskWorker.STATUS_SKIPPED])).count()
        remaining_assignments = task.project.repetition - assignments_completed
        if remaining_assignments > 0 and mturk_hit.num_assignments == mturk_hit.mturk_assignments. \
            filter(status=TaskWorker.STATUS_SUBMITTED).count() and \
                mturk_hit.mturk_assignments.filter(status=TaskWorker.STATUS_IN_PROGRESS).count() == 0:
            self.add_assignments(hit_id=mturk_hit.hit_id, increment=1)
            self.extend_hit(hit_id=mturk_hit.hit_id)
            mturk_hit.status = MTurkHIT.STATUS_IN_PROGRESS
            mturk_hit.num_assignments += 1
            mturk_hit.save()
        elif remaining_assignments == 0:
            self.expire_hit(hit_id=mturk_hit.hit_id)
            mturk_hit.status = MTurkHIT.STATUS_EXPIRED
            mturk_hit.save()
        elif remaining_assignments > 0 and \
                mturk_hit.status == MTurkHIT.STATUS_EXPIRED:
            self.extend_hit(hit_id=mturk_hit.hit_id)
            mturk_hit.status = MTurkHIT.STATUS_IN_PROGRESS
        return 'SUCCESS'

    def get_assignment(self, assignment_id):
        try:
            return self.connection.get_assignment(assignment_id)[0], True
        except MTurkRequestError as e:
            error = e.errors[0][0]
            if error == 'AWS.MechanicalTurk.InvalidAssignmentState':
                return assignment_id, False
            return None, False

    def set_notification(self, hit_type_id):
        self.connection.set_rest_notification(hit_type=hit_type_id,
                                              url=self.host + '/api/mturk/notification',
                                              event_types=['AssignmentReturned', 'AssignmentAbandoned',
                                                           'AssignmentAccepted', 'AssignmentSubmitted'])

    def approve_assignment(self, task_worker):
        task_worker_obj = TaskWorker.objects.get(id=task_worker['id'])
        if hasattr(task_worker_obj, 'mturk_assignments') and task_worker_obj.mturk_assignments.first() is not None:
            try:
                self.connection.approve_assignment(task_worker_obj.mturk_assignments.first().assignment_id)
            except MTurkRequestError:
                pass
        return 'SUCCESS'

    def expire_hit(self, hit_id):
        try:
            self.connection.expire_hit(hit_id)
        except MTurkRequestError:
            pass

    def extend_hit(self, hit_id):
        try:
            self.connection.extend_hit(hit_id=hit_id, expiration_increment=604800)  # 7 days
        except MTurkRequestError:
            pass

    def add_assignments(self, hit_id, increment=1):
        try:
            self.connection.extend_hit(hit_id=hit_id, assignments_increment=increment)
        except MTurkRequestError:
            pass

    def test_connection(self):
        try:
            return self.connection.get_account_balance()[0], True
        except MTurkRequestError as e:
            error = e.errors[0][0]
            if error == 'AWS.NotAuthorized':
                return None, False
            return None, False


class MultiLocaleRequirement(LocaleRequirement):
    def __init__(self, comparator, locale, required_to_preview=False):
        super(MultiLocaleRequirement, self).__init__(comparator=comparator, locale=locale,
                                                     required_to_preview=required_to_preview)

    def get_as_params(self):
        params = {
            "QualificationTypeId": self.qualification_type_id,
            "Comparator": self.comparator
        }
        locales = {}
        if isinstance(self.locale, list):
            for index, country in enumerate(self.locale):
                locales['LocaleValue.%s.Country' % (index + 1)] = country
        params.update(locales)
        return params
