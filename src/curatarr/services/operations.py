"""Execute registered provider steps through fresh inspection and durable write intent."""
from dataclasses import dataclass
from enum import Enum

from curatarr.adapters import AdapterError
from curatarr.storage import StorageError


class Outcome(str, Enum):
    VERIFIED = 'verified'
    NEEDS_WRITE = 'needs_write'
    PENDING = 'pending'
    CONFLICT = 'conflict'


@dataclass(frozen=True)
class Inspection:
    outcome: Outcome
    evidence: dict


@dataclass(frozen=True)
class OperationStep:
    name: str
    inspect: object
    apply: object | None = None


class OperationRunner:
    """Only application-registered actions run; payloads cannot select arbitrary calls."""

    def __init__(self, journal, plans):
        self.journal, self.plans = journal, plans

    def run(self, identifier):
        claim = self.journal.claim_operation(identifier)
        if claim is None:
            return self.journal.operation(identifier)
        try:
            operation = self.journal.owned(claim)
            plan = self.plans.get(operation['kind'])
            if plan is None or [s.name for s in plan] != [s['name'] for s in operation['steps']]:
                self.journal.defer_operation(claim, 'unsupported_operation_plan', uncertain=False)
                return self.journal.operation(identifier)
            for action in plan:
                operation = self.journal.owned(claim)
                step = next(s for s in operation['steps'] if s['name'] == action.name)
                inspection = action.inspect(operation)
                if not isinstance(inspection, Inspection):
                    raise ValueError('Invalid provider inspection.')
                if inspection.outcome is Outcome.VERIFIED:
                    self.journal.step_result(claim, action.name, verified=True, evidence=inspection.evidence)
                    continue
                if inspection.outcome is Outcome.CONFLICT or step['status'] == 'verified':
                    self.journal.defer_operation(claim, 'provider_state_changed', uncertain=False)
                    return self.journal.operation(identifier)
                if inspection.outcome is Outcome.PENDING or step['status'] != 'planned':
                    self.journal.defer_operation(claim, 'provider_outcome_pending')
                    return self.journal.operation(identifier)
                if inspection.outcome is not Outcome.NEEDS_WRITE or action.apply is None:
                    self.journal.defer_operation(claim, 'provider_action_unavailable', uncertain=False)
                    return self.journal.operation(identifier)
                if not self.journal.begin_step(claim, action.name):
                    raise StorageError('Operation step was already attempted.')
                # Check fencing immediately before the external call. Upstream APIs
                # do not implement our lease: uncertain prior effects still reserve
                # this movie until inspected and resolved, even after lease expiry.
                self.journal.owned(claim)
                result = action.apply(self.journal.operation(identifier))
                self.journal.step_result(claim, action.name, verified=False,
                                         evidence={'response_received': True, 'result': result if isinstance(result, dict) else {}})
                inspection = action.inspect(self.journal.owned(claim))
                if not isinstance(inspection, Inspection):
                    raise ValueError('Invalid provider inspection.')
                if inspection.outcome is Outcome.VERIFIED:
                    self.journal.step_result(claim, action.name, verified=True, evidence=inspection.evidence)
                else:
                    self.journal.defer_operation(claim, 'provider_outcome_pending')
                    return self.journal.operation(identifier)
            return self.journal.finish_operation(claim)
        except (AdapterError, ValueError, KeyError, TypeError):
            # Persist only a fixed classification, never upstream URLs/credentials.
            try:
                current = self.journal.owned(claim)
                uncertain = any(s['status'] in {'attempted', 'accepted'} for s in current['steps'])
                self.journal.defer_operation(claim, 'provider_check_unavailable', uncertain=uncertain)
            except StorageError:
                pass  # A new owner alone can publish state after a lease change.
            return self.journal.operation(identifier)
