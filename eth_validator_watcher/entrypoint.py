"""Entrypoint for the eth-validator-watcher CLI."""

from os import environ
from pathlib import Path
from time import sleep, time
from typing import List, Optional

import typer
from prometheus_client import Gauge, start_http_server
from typer import Option

from .beacon import Beacon
from .coinbase import Coinbase
from .entry_queue import export_duration_sec as export_entry_queue_dur_sec
from .execution import Execution
from .exited_validators import ExitedValidators
from .fee_recipient import process_fee_recipient
from .missed_attestations import (
    process_double_missed_attestations,
    process_missed_attestations,
)
from .missed_blocks import process_missed_blocks
from .models import BeaconType, Validators
from .next_blocks_proposal import process_future_blocks_proposal
from .slashed_validators import SlashedValidators
from .suboptimal_attestations import process_suboptimal_attestations
from .utils import (
    BLOCK_NOT_ORPHANED_TIME_SEC,
    NB_SLOT_PER_EPOCH,
    SLOT_FOR_MISSED_ATTESTATIONS_PROCESS,
    SLOT_FOR_REWARDS_PROCESS,
    LimitedDict,
    Slack,
    get_our_pubkeys,
    slots,
    write_liveness_file,
    eth1_address_0x_prefixed,
)

from .rewards import process_rewards
from .web3signer import Web3Signer

from .relays import Relays

Status = Validators.DataItem.StatusEnum


app = typer.Typer(add_completion=False)

slot_gauge = Gauge("slot", "Slot")
epoch_gauge = Gauge("epoch", "Epoch")

our_queued_vals_gauge = Gauge(
    "our_pending_queued_validators_count",
    "Our pending queued validators count",
)

net_pending_q_vals_gauge = Gauge(
    "total_pending_queued_validators_count",
    "Total pending queued validators count",
)

our_active_validators_gauge = Gauge(
    "our_active_validators_count",
    "Our active validators count",
)

net_active_validators_gauge = Gauge(
    "total_active_validators_count",
    "Total active validators count",
)


@app.command()
def handler(
    beacon_url: str = Option(..., help="URL of beacon node", show_default=False),
    execution_url: str = Option(None, help="URL of execution node", show_default=False),
    pubkeys_file_path: Optional[Path] = Option(
        None,
        help="File containing the list of public keys to watch",
        exists=True,
        file_okay=True,
        dir_okay=False,
        show_default=False,
    ),
    web3signer_url: Optional[str] = Option(
        None, help="URL to web3signer managing keys to watch", show_default=False
    ),
    fee_recipient: Optional[str] = Option(
        None,
        help="Fee recipient address - --execution-url must be set",
        show_default=False,
    ),
    slack_channel: Optional[str] = Option(
        None,
        help="Slack channel to send alerts - SLACK_TOKEN env var must be set",
        show_default=False,
    ),
    beacon_type: BeaconType = Option(
        BeaconType.OTHER,
        case_sensitive=False,
        help=(
            "Use this option if connected to a Teku < 23.6.0, Prysm, Lighthouse or "
            "Nimbus beacon node. "
            "See https://github.com/ConsenSys/teku/issues/7204 for Teku < 23.6.0, "
            "https://github.com/prysmaticlabs/prysm/issues/11581 for Prysm, "
            "https://github.com/sigp/lighthouse/issues/4243 for Lighthouse, "
            "https://github.com/status-im/nimbus-eth2/issues/5019 and "
            "https://github.com/status-im/nimbus-eth2/issues/5138 for Nimbus."
        ),
        show_default=False,
    ),
    relay_url: List[str] = Option(
        [], help="URL of allow listed relay", show_default=False
    ),
    liveness_file: Optional[Path] = Option(
        None, help="Liveness file", show_default=False
    ),
) -> None:
    """
    🚨 Ethereum Validator Watcher 🚨

    \b
    Ethereum Validator Watcher monitors the Ethereum beacon chain in real-time and notifies you when any of your validators:
    - are going to propose a block in the next two epochs
    - missed a block proposal
    - did not optimally attest
    - missed an attestation
    - missed two attestations in a row
    - proposed a block with the wrong fee recipient
    - has exited
    - got slashed
    - proposed a block with an unknown relay
    - did not had optimal source, target or head reward

    \b
    It also exports some general metrics such as:
    - your USD assets under management
    - the total staking market cap
    - epoch and slot
    - the number or total slashed validators
    - ETH/USD conversion rate
    - the number of your queued validators
    - the number of your active validators
    - the number of your exited validators
    - the number of the network queued validators
    - the number of the network active validators
    - the entry queue duration estimation

    \b
    Optionally, you can specify the following parameters:
    - the path to a file containing the list of public your keys to watch, or / and
    - a URL to a Web3Signer instance managing your keys to watch.

    \b
    Pubkeys are dynamically loaded, at each epoch start.
    - If you use pubkeys file, you can change it without having to restart the watcher.
    - If you use Web3Signer, a request to Web3Signer is done at every epoch to get the
    latest set of keys to watch.

    \b
    Finally, this program exports the following sets of data from:
    - Prometheus (you can use a Grafana dashboard to monitor your validators)
    - Slack
    - logs

    Prometheus server is automatically exposed on port 8000.
    """
    _handler(  # pragma: no cover
        beacon_url,
        execution_url,
        pubkeys_file_path,
        web3signer_url,
        fee_recipient,
        slack_channel,
        beacon_type,
        relay_url,
        liveness_file,
    )


def _handler(
    beacon_url: str,
    execution_url: Optional[str],
    pubkeys_file_path: Optional[Path],
    web3signer_url: Optional[str],
    fee_recipient: Optional[str],
    slack_channel: Optional[str],
    beacon_type: BeaconType,
    relays_url: List[str],
    liveness_file: Optional[Path],
) -> None:
    """Just a wrapper to be able to test the handler function"""
    slack_token = environ.get("SLACK_TOKEN")

    if fee_recipient is not None and execution_url is None:
        raise typer.BadParameter(
            "`execution-url` must be set if you want to use `fee-recipient`"
        )

    if fee_recipient is not None:
        try:
            fee_recipient = eth1_address_0x_prefixed(fee_recipient)
        except ValueError:
            raise typer.BadParameter("`fee-recipient` should be a valid ETH1 address")

    if slack_channel is not None and slack_token is None:
        raise typer.BadParameter(
            "SLACK_TOKEN env var must be set if you want to use `slack-channel`"
        )

    slack = (
        Slack(slack_channel, slack_token)
        if slack_channel is not None and slack_token is not None
        else None
    )

    start_http_server(8000)

    beacon = Beacon(beacon_url)
    execution = Execution(execution_url) if execution_url is not None else None
    coinbase = Coinbase()
    web3signer = Web3Signer(web3signer_url) if web3signer_url is not None else None
    relays = Relays(relays_url)

    our_pubkeys: set[str] = set()
    our_active_idx2val: dict[int, Validators.DataItem.Validator] = {}
    our_validators_indexes_that_missed_attestation: set[int] = set()
    our_validators_indexes_that_missed_previous_attestation: set[int] = set()
    our_epoch2active_idx2val = LimitedDict(3)
    net_epoch2active_idx2val = LimitedDict(3)

    exited_validators = ExitedValidators(slack)
    slashed_validators = SlashedValidators(slack)

    last_missed_attestations_process_epoch: Optional[int] = None
    last_rewards_process_epoch: Optional[int] = None

    previous_epoch: Optional[int] = None
    genesis = beacon.get_genesis()

    for slot, slot_start_time_sec in slots(genesis.data.genesis_time):
        epoch = slot // NB_SLOT_PER_EPOCH
        slot_in_epoch = slot % NB_SLOT_PER_EPOCH

        slot_gauge.set(slot)
        epoch_gauge.set(epoch)

        is_new_epoch = previous_epoch is None or previous_epoch != epoch

        if is_new_epoch:
            try:
                our_pubkeys = get_our_pubkeys(pubkeys_file_path, web3signer)
            except ValueError:
                raise typer.BadParameter("Some pubkeys are invalid")

            # Network validators
            # ------------------
            net_status2idx2val = beacon.get_status_to_index_to_validator()

            net_pending_q_idx2val = net_status2idx2val.get(Status.pendingQueued, {})
            nb_total_pending_q_vals = len(net_pending_q_idx2val)
            net_pending_q_vals_gauge.set(nb_total_pending_q_vals)

            active_ongoing = net_status2idx2val.get(Status.activeOngoing, {})
            active_exiting = net_status2idx2val.get(Status.activeExiting, {})
            active_slashed = net_status2idx2val.get(Status.activeSlashed, {})
            net_active_idx2val = active_ongoing | active_exiting | active_slashed
            net_epoch2active_idx2val[epoch] = net_active_idx2val

            net_active_vals_count = len(net_active_idx2val)
            net_active_validators_gauge.set(net_active_vals_count)

            net_exited_s_idx2val = net_status2idx2val.get(Status.exitedSlashed, {})

            with_poss = net_status2idx2val.get(Status.withdrawalPossible, {})
            with_done = net_status2idx2val.get(Status.withdrawalDone, {})
            net_withdrawable_idx2val = with_poss | with_done

            # Our validators
            # --------------
            our_status2idx2val = {
                status: {
                    index: validator
                    for index, validator in validator.items()
                    if validator.pubkey in our_pubkeys
                }
                for status, validator in net_status2idx2val.items()
            }

            our_queued_idx2val = our_status2idx2val.get(Status.pendingQueued, {})
            our_queued_vals_gauge.set(len(our_queued_idx2val))

            ongoing = our_status2idx2val.get(Status.activeOngoing, {})
            active_exiting = our_status2idx2val.get(Status.activeExiting, {})
            active_slashed = our_status2idx2val.get(Status.activeSlashed, {})
            our_active_idx2val = ongoing | active_exiting | active_slashed
            our_epoch2active_idx2val[epoch] = our_active_idx2val

            our_active_validators_gauge.set(len(our_active_idx2val))
            our_exited_u_idx2val = our_status2idx2val.get(Status.exitedUnslashed, {})
            our_exited_s_idx2val = our_status2idx2val.get(Status.exitedSlashed, {})

            with_poss = our_status2idx2val.get(Status.withdrawalPossible, {})
            with_done = our_status2idx2val.get(Status.withdrawalDone, {})
            our_withdrawable_idx2val = with_poss | with_done

            exited_validators.process(our_exited_u_idx2val, our_withdrawable_idx2val)

            slashed_validators.process(
                net_exited_s_idx2val,
                our_exited_s_idx2val,
                net_withdrawable_idx2val,
                our_withdrawable_idx2val,
            )

            export_entry_queue_dur_sec(net_active_vals_count, nb_total_pending_q_vals)
            coinbase.emit_eth_usd_conversion_rate()

        if previous_epoch is not None and previous_epoch != epoch:
            print(f"🎂     Epoch     {epoch}     starts")

        should_process_missed_attestations = (
            slot_in_epoch >= SLOT_FOR_MISSED_ATTESTATIONS_PROCESS
            and (
                last_missed_attestations_process_epoch is None
                or last_missed_attestations_process_epoch != epoch
            )
        )

        if should_process_missed_attestations:
            our_validators_indexes_that_missed_attestation = (
                process_missed_attestations(
                    beacon, beacon_type, our_epoch2active_idx2val, epoch
                )
            )

            process_double_missed_attestations(
                our_validators_indexes_that_missed_attestation,
                our_validators_indexes_that_missed_previous_attestation,
                our_epoch2active_idx2val,
                epoch,
                slack,
            )

            last_missed_attestations_process_epoch = epoch

        is_slot_big_enough = slot_in_epoch >= SLOT_FOR_REWARDS_PROCESS
        is_last_rewards_epoch_none = last_rewards_process_epoch is None
        is_new_rewards_epoch = last_rewards_process_epoch != epoch
        epoch_condition = is_last_rewards_epoch_none or is_new_rewards_epoch
        should_process_rewards = is_slot_big_enough and epoch_condition

        if should_process_rewards:
            process_rewards(
                beacon,
                beacon_type,
                epoch,
                net_epoch2active_idx2val,
                our_epoch2active_idx2val,
            )

            last_rewards_process_epoch = epoch

        process_future_blocks_proposal(beacon, our_pubkeys, slot, is_new_epoch)

        delta_sec = BLOCK_NOT_ORPHANED_TIME_SEC - (time() - slot_start_time_sec)
        sleep(max(0, delta_sec))

        potential_block = beacon.get_potential_block(slot)

        if potential_block is not None:
            block = potential_block

            process_suboptimal_attestations(
                beacon,
                block,
                slot,
                our_active_idx2val,
            )

            process_fee_recipient(
                block, our_active_idx2val, execution, fee_recipient, slack
            )

        is_our_validator = process_missed_blocks(
            beacon,
            potential_block,
            slot,
            our_pubkeys,
            slack,
        )

        if is_our_validator and potential_block is not None:
            relays.process(slot)

        our_validators_indexes_that_missed_previous_attestation = (
            our_validators_indexes_that_missed_attestation
        )

        previous_epoch = epoch

        if slot_in_epoch >= SLOT_FOR_MISSED_ATTESTATIONS_PROCESS:
            should_process_missed_attestations = True

        if liveness_file is not None:
            write_liveness_file(liveness_file)
