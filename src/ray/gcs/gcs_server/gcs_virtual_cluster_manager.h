#pragma once

#include "ray/common/asio/instrumented_io_context.h"
#include "ray/gcs/gcs_server/gcs_node_manager.h"
#include "ray/raylet/scheduling/cluster_resource_scheduler.h"
#include "ray/rpc/gcs_server/gcs_rpc_server.h"
#include "ray/rpc/node_manager/node_manager_client_pool.h"
#include "src/ray/protobuf/gcs_service.pb.h"

namespace ray {
namespace gcs {
class GcsVirtualClusterManager : public rpc::VirtualClusterInfoHandler {
 public:
  GcsVirtualClusterManager(
      instrumented_io_context &io_context,
      const gcs::GcsNodeManager &gcs_node_manager,
      ClusterResourceScheduler &cluster_resource_scheduler,
      std::shared_ptr<rpc::NodeManagerClientPool> raylet_client_pool);

  ~GcsVirtualClusterManager() = default;

  void HandleCreateVirtualCluster(rpc::CreateVirtualClusterRequest request,
                                  rpc::CreateVirtualClusterReply *reply,
                                  rpc::SendReplyCallback send_reply_callback) override;

  void HandleRemoveVirtualCluster(rpc::RemoveVirtualClusterRequest request,
                                  rpc::RemoveVirtualClusterReply *reply,
                                  rpc::SendReplyCallback send_reply_callback) override;

 private:
  std::unordered_map<NodeID, VirtualClusterBundleSpec> Schedule(
      const rpc::CreateVirtualClusterRequest &request);

  std::shared_ptr<ResourceReserveInterface> GetLeaseClientFromNode(
      const std::shared_ptr<rpc::GcsNodeInfo> &node);

  instrumented_io_context &io_context_;
  const GcsNodeManager &gcs_node_manager_;
  ClusterResourceScheduler &cluster_resource_scheduler_;
  std::shared_ptr<rpc::NodeManagerClientPool> raylet_client_pool_;
};
}  // namespace gcs
}  // namespace ray
