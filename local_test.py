import os
import sys
import torch
import multiprocessing as mp

# 指定使用 GPU
os.environ["CUDA_VISIBLE_DEVICES"] = "0"

def run_collaborator(queue=None):
    agent_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VOGS_Collaborator_Agent")
    sys.path.insert(0, agent_dir)
    os.chdir(agent_dir)
    from fast_api.model_runtime import model_runtime as collab_runtime
    import pickle
    
    print("Loading Collaborator Model...")
    collab_runtime.load_model("Latency_Test/baseline/collab")
    collab_runtime.opt.num_frames = 64
    
    def mock_send(payload):
        frame_id = payload.get('frame_id')
        print(f"[Collaborator] Sending payload for frame {frame_id}")
        with open(f"/tmp/payload_{frame_id}.pkl", "wb") as f:
            pickle.dump(payload, f)
        
    print("Running Collaborator...")
    collab_result = collab_runtime.run_benchmark_sync(mock_send)
    print("Collaborator Result:", collab_result)


def run_ego(queue=None):
    agent_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VOGS_Ego_Agent")
    sys.path.insert(0, agent_dir)
    os.chdir(agent_dir)
    from fast_api.model_runtime import model_runtime as ego_runtime
    import pickle
    
    print("Loading Ego Model...")
    ego_runtime.load_model("Latency_Test/baseline/collab")
    ego_runtime.opt.num_frames = 64
    
    # We need a counter because mock_receive is called per frame
    frame_counter = [0]
    
    def mock_receive():
        frame_id = frame_counter[0]
        frame_counter[0] += 1
        print(f"[Ego] Received payload for frame {frame_id}")
        with open(f"/tmp/payload_{frame_id}.pkl", "rb") as f:
            payload = pickle.load(f)
        return payload
        
    print("Running Ego...")
    ego_result = ego_runtime.run_benchmark_sync(mock_receive)
    print("Ego Result:", ego_result)

def main():
    mp.set_start_method('spawn')
    
    p_collab = mp.Process(target=run_collaborator)
    p_ego = mp.Process(target=run_ego)
    
    p_collab.start()
    p_collab.join()
    
    p_ego.start()
    p_ego.join()
    
    print("Simulation finished!")

if __name__ == "__main__":
    main()
