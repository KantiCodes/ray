import sys

import pytest

import ray
from ray._raylet import GcsClient
from ray.tests.conftest import *  # noqa
from ray.serve._private.deployment_scheduler import (
    DeploymentScheduler,
    SpreadDeploymentSchedulingPolicy,
    DriverDeploymentSchedulingPolicy,
    ReplicaSchedulingRequest,
    DeploymentDownscaleRequest,
)
from ray.serve._private.utils import get_head_node_id
from ray.serve._private.default_impl import (
    create_cluster_node_info_cache,
)


@ray.remote(num_cpus=1)
class Replica:
    def get_node_id(self):
        return ray.get_runtime_context().get_node_id()

    def get_placement_group(self):
        return ray.util.get_current_placement_group()


@pytest.mark.parametrize(
    "placement_group_config",
    [
        {},
        {"bundles": [{"CPU": 3}]},
        {"bundles": [{"CPU": 1}, {"CPU": 1}, {"CPU": 1}], "strategy": "STRICT_PACK"},
    ],
)
def test_spread_deployment_scheduling_policy_upscale(
    ray_start_cluster, placement_group_config
):
    """Test to make sure replicas are spreaded."""
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=3)
    cluster.add_node(num_cpus=3)
    cluster.wait_for_nodes()
    ray.init(address=cluster.address)

    cluster_node_info_cache = create_cluster_node_info_cache(
        GcsClient(address=ray.get_runtime_context().gcs_address)
    )
    cluster_node_info_cache.update()

    scheduler = DeploymentScheduler(cluster_node_info_cache)
    scheduler.on_deployment_created("deployment1", SpreadDeploymentSchedulingPolicy())
    replica_actor_handles = []
    replica_placement_groups = []

    def on_scheduled(actor_handle, placement_group):
        replica_actor_handles.append(actor_handle)
        replica_placement_groups.append(placement_group)

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={
            "deployment1": [
                ReplicaSchedulingRequest(
                    deployment_name="deployment1",
                    replica_name="replica1",
                    actor_def=Replica,
                    actor_resources={"CPU": 1},
                    actor_options={"name": "deployment1_replica1"},
                    actor_init_args=(),
                    on_scheduled=on_scheduled,
                    placement_group_bundles=placement_group_config.get("bundles", None),
                    placement_group_strategy=placement_group_config.get(
                        "strategy", None
                    ),
                ),
                ReplicaSchedulingRequest(
                    deployment_name="deployment1",
                    replica_name="replica2",
                    actor_def=Replica,
                    actor_resources={"CPU": 1},
                    actor_options={"name": "deployment1_replica2"},
                    actor_init_args=(),
                    on_scheduled=on_scheduled,
                    placement_group_bundles=placement_group_config.get("bundles", None),
                    placement_group_strategy=placement_group_config.get(
                        "strategy", None
                    ),
                ),
            ]
        },
        downscales={},
    )
    assert not deployment_to_replicas_to_stop
    assert len(replica_actor_handles) == 2
    assert len(replica_placement_groups) == 2
    assert not scheduler._pending_replicas["deployment1"]
    assert len(scheduler._launching_replicas["deployment1"]) == 2
    assert (
        len(
            {
                ray.get(replica_actor_handles[0].get_node_id.remote()),
                ray.get(replica_actor_handles[1].get_node_id.remote()),
            }
        )
        == 2
    )
    if "bundles" in placement_group_config:
        assert (
            len(
                {
                    ray.get(replica_actor_handles[0].get_placement_group.remote()),
                    ray.get(replica_actor_handles[1].get_placement_group.remote()),
                }
            )
            == 2
        )
    scheduler.on_replica_stopping("deployment1", "replica1")
    scheduler.on_replica_stopping("deployment1", "replica2")
    scheduler.on_deployment_deleted("deployment1")


def test_spread_deployment_scheduling_policy_downscale_multiple_deployments(
    ray_start_cluster,
):
    """Test to make sure downscale prefers replicas without node id
    and then replicas on a node with fewest replicas of all deployments.
    """
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=3)
    cluster.wait_for_nodes()
    ray.init(address=cluster.address)

    cluster_node_info_cache = create_cluster_node_info_cache(
        GcsClient(address=ray.get_runtime_context().gcs_address)
    )
    cluster_node_info_cache.update()

    scheduler = DeploymentScheduler(cluster_node_info_cache)
    scheduler.on_deployment_created("deployment1", SpreadDeploymentSchedulingPolicy())
    scheduler.on_deployment_created("deployment2", SpreadDeploymentSchedulingPolicy())
    scheduler.on_replica_running("deployment1", "replica1", "node1")
    scheduler.on_replica_running("deployment1", "replica2", "node2")
    scheduler.on_replica_running("deployment1", "replica3", "node2")
    scheduler.on_replica_running("deployment2", "replica1", "node1")
    scheduler.on_replica_running("deployment2", "replica2", "node2")
    scheduler.on_replica_running("deployment2", "replica3", "node1")
    scheduler.on_replica_running("deployment2", "replica4", "node1")
    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    # Even though node1 has fewest replicas of deployment1
    # but it has more replicas of all deployments so
    # we should stop replicas from node2.
    assert len(deployment_to_replicas_to_stop["deployment1"]) == 1
    assert deployment_to_replicas_to_stop["deployment1"] < {"replica2", "replica3"}

    scheduler.on_replica_stopping("deployment1", "replica3")
    scheduler.on_replica_stopping("deployment2", "replica3")
    scheduler.on_replica_stopping("deployment2", "replica4")

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            ),
            "deployment2": DeploymentDownscaleRequest(
                deployment_name="deployment2", num_to_stop=1
            ),
        },
    )
    assert len(deployment_to_replicas_to_stop) == 2
    # We should stop replicas from the same node.
    assert len(deployment_to_replicas_to_stop["deployment1"]) == 1
    assert (
        deployment_to_replicas_to_stop["deployment1"]
        == deployment_to_replicas_to_stop["deployment2"]
    )

    scheduler.on_replica_stopping("deployment1", "replica1")
    scheduler.on_replica_stopping("deployment1", "replica2")
    scheduler.on_replica_stopping("deployment2", "replica1")
    scheduler.on_replica_stopping("deployment2", "replica2")
    scheduler.on_deployment_deleted("deployment1")
    scheduler.on_deployment_deleted("deployment2")


def test_spread_deployment_scheduling_policy_downscale_single_deployment(
    ray_start_cluster,
):
    """Test to make sure downscale prefers replicas without node id
    and then replicas on a node with fewest replicas of all deployments.
    """
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=3)
    cluster.wait_for_nodes()
    ray.init(address=cluster.address)

    cluster_node_info_cache = create_cluster_node_info_cache(
        GcsClient(address=ray.get_runtime_context().gcs_address)
    )
    cluster_node_info_cache.update()

    scheduler = DeploymentScheduler(cluster_node_info_cache)
    scheduler.on_deployment_created("deployment1", SpreadDeploymentSchedulingPolicy())
    scheduler.on_replica_running("deployment1", "replica1", "node1")
    scheduler.on_replica_running("deployment1", "replica2", "node1")
    scheduler.on_replica_running("deployment1", "replica3", "node2")
    scheduler.on_replica_recovering("deployment1", "replica4")
    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    # Prefer replica without node id
    assert deployment_to_replicas_to_stop["deployment1"] == {"replica4"}
    scheduler.on_replica_stopping("deployment1", "replica4")

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={
            "deployment1": [
                ReplicaSchedulingRequest(
                    deployment_name="deployment1",
                    replica_name="replica5",
                    actor_def=Replica,
                    actor_resources={"CPU": 1},
                    actor_options={},
                    actor_init_args=(),
                    on_scheduled=lambda actor_handle, placement_group: actor_handle,
                ),
            ]
        },
        downscales={},
    )
    assert not deployment_to_replicas_to_stop
    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    # Prefer replica without node id
    assert deployment_to_replicas_to_stop["deployment1"] == {"replica5"}
    scheduler.on_replica_stopping("deployment1", "replica5")

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    # Prefer replica on a node with fewest replicas of all deployments.
    assert deployment_to_replicas_to_stop["deployment1"] == {"replica3"}
    scheduler.on_replica_stopping("deployment1", "replica3")

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=2
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    assert deployment_to_replicas_to_stop["deployment1"] == {"replica1", "replica2"}
    scheduler.on_replica_stopping("deployment1", "replica1")
    scheduler.on_replica_stopping("deployment1", "replica2")
    scheduler.on_deployment_deleted("deployment1")


def test_spread_deployment_scheduling_policy_downscale_head_node(ray_start_cluster):
    """Test to make sure downscale deprioritizes replicas on the head node."""
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=3)
    cluster.wait_for_nodes()
    ray.init(address=cluster.address)
    head_node_id = get_head_node_id()

    cluster_node_info_cache = create_cluster_node_info_cache(
        GcsClient(address=ray.get_runtime_context().gcs_address)
    )
    cluster_node_info_cache.update()

    scheduler = DeploymentScheduler(cluster_node_info_cache)
    scheduler.on_deployment_created("deployment1", SpreadDeploymentSchedulingPolicy())
    scheduler.on_replica_running("deployment1", "replica1", head_node_id)
    scheduler.on_replica_running("deployment1", "replica2", "node2")
    scheduler.on_replica_running("deployment1", "replica3", "node2")
    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    assert deployment_to_replicas_to_stop["deployment1"] < {"replica2", "replica3"}
    scheduler.on_replica_stopping(
        "deployment1", deployment_to_replicas_to_stop["deployment1"].pop()
    )

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    assert deployment_to_replicas_to_stop["deployment1"] < {"replica2", "replica3"}
    scheduler.on_replica_stopping(
        "deployment1", deployment_to_replicas_to_stop["deployment1"].pop()
    )

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={},
        downscales={
            "deployment1": DeploymentDownscaleRequest(
                deployment_name="deployment1", num_to_stop=1
            )
        },
    )
    assert len(deployment_to_replicas_to_stop) == 1
    assert deployment_to_replicas_to_stop["deployment1"] == {"replica1"}
    scheduler.on_replica_stopping("deployment1", "replica1")
    scheduler.on_deployment_deleted("deployment1")


def test_driver_deployment_scheduling_policy_upscale(ray_start_cluster):
    """Test to make sure there is only one replica on each node
    for the driver deployment.
    """
    cluster = ray_start_cluster
    cluster.add_node(num_cpus=3)
    cluster.add_node(num_cpus=3)
    cluster.wait_for_nodes()
    ray.init(address=cluster.address)

    cluster_node_info_cache = create_cluster_node_info_cache(
        GcsClient(address=ray.get_runtime_context().gcs_address)
    )
    cluster_node_info_cache.update()

    scheduler = DeploymentScheduler(cluster_node_info_cache)
    scheduler.on_deployment_created("deployment1", DriverDeploymentSchedulingPolicy())

    replica_actor_handles = []

    def on_scheduled(actor_handle, placement_group):
        replica_actor_handles.append(actor_handle)

    deployment_to_replicas_to_stop = scheduler.schedule(
        upscales={
            "deployment1": [
                ReplicaSchedulingRequest(
                    deployment_name="deployment1",
                    replica_name="replica1",
                    actor_def=Replica,
                    actor_resources={"CPU": 1},
                    actor_options={},
                    actor_init_args=(),
                    on_scheduled=on_scheduled,
                ),
                ReplicaSchedulingRequest(
                    deployment_name="deployment1",
                    replica_name="replica2",
                    actor_def=Replica,
                    actor_resources={"CPU": 1},
                    actor_options={},
                    actor_init_args=(),
                    on_scheduled=on_scheduled,
                ),
                ReplicaSchedulingRequest(
                    deployment_name="deployment1",
                    replica_name="replica3",
                    actor_def=Replica,
                    actor_resources={"CPU": 1},
                    actor_options={},
                    actor_init_args=(),
                    on_scheduled=on_scheduled,
                ),
            ]
        },
        downscales={},
    )
    assert not deployment_to_replicas_to_stop
    # 2 out of 3 replicas are scheduled since there are only two nodes in the cluster.
    assert len(replica_actor_handles) == 2
    assert len(scheduler._pending_replicas["deployment1"]) == 1
    assert len(scheduler._launching_replicas["deployment1"]) == 2
    assert (
        len(
            {
                ray.get(replica_actor_handles[0].get_node_id.remote()),
                ray.get(replica_actor_handles[1].get_node_id.remote()),
            }
        )
        == 2
    )

    scheduler.on_replica_recovering("deployment1", "replica4")
    cluster.add_node(num_cpus=3)
    cluster.wait_for_nodes()
    cluster_node_info_cache.update()

    deployment_to_replicas_to_stop = scheduler.schedule(upscales={}, downscales={})
    assert not deployment_to_replicas_to_stop
    # No schduling while some replica is recovering
    assert len(replica_actor_handles) == 2

    scheduler.on_replica_stopping("deployment1", "replica4")
    # The last replica is scheduled
    deployment_to_replicas_to_stop = scheduler.schedule(upscales={}, downscales={})
    assert not deployment_to_replicas_to_stop
    assert not scheduler._pending_replicas["deployment1"]
    assert len(scheduler._launching_replicas["deployment1"]) == 3
    assert len(replica_actor_handles) == 3
    assert (
        len(
            {
                ray.get(replica_actor_handles[0].get_node_id.remote()),
                ray.get(replica_actor_handles[1].get_node_id.remote()),
                ray.get(replica_actor_handles[2].get_node_id.remote()),
            }
        )
        == 3
    )

    scheduler.on_replica_stopping("deployment1", "replica1")
    scheduler.on_replica_stopping("deployment1", "replica2")
    scheduler.on_replica_stopping("deployment1", "replica3")
    scheduler.on_deployment_deleted("deployment1")


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))