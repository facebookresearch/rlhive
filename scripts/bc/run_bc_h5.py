"""
Job script to learn policy using BC
"""

import os
import time
from os import environ
environ['CUDA_DEVICE_ORDER']='PCI_BUS_ID'
environ['MKL_THREADING_LAYER']='GNU'
import pickle
import yaml
import hydra
import gym
import wandb
import numpy as np
from omegaconf import DictConfig, OmegaConf, ListConfig

from batch_norm_mlp import BatchNormMLP
from gmm_policy import GMMPolicy
from behavior_cloning import BC
from misc import control_seed, \
        bcolors, stack_tensor_dict_list
from torchrl.record.loggers.wandb import WandbLogger
from robohive.logger.grouped_datasets import Trace as Trace

def evaluate_policy(
            policy,
            env,
            num_episodes,
            epoch,
            horizon=None,
            gamma=1,
            percentile=[],
            get_full_dist=False,
            eval_logger=None,
            device='cpu',
            seed=123,
            verbose=True,
    ):
    env.seed(seed)
    horizon = env.horizon if horizon is None else horizon
    mean_eval, std, min_eval, max_eval = 0.0, 0.0, -1e8, -1e8
    ep_returns = np.zeros(num_episodes)
    policy.eval()
    paths = []

    for ep in range(num_episodes):
        observations=[]
        actions=[]
        rewards=[]
        agent_infos = []
        env_infos = []
        o = env.reset()
        t, done = 0, False
        while t < horizon and (done == False):
            a = policy.get_action(o)[1]['evaluation']
            next_o, r, done, env_info = env.step(a)
            ep_returns[ep] += (gamma ** t) * r
            observations.append(o)
            actions.append(a)
            rewards.append(r)
            agent_infos.append(None)
            env_infos.append(env_info)
            o = next_o
            t += 1
        if verbose:
            print("Episode: {}; Reward: {}".format(ep, ep_returns[ep]))
        path = dict(
            observations=np.array(observations),
            actions=np.array(actions),
            rewards=np.array(rewards),
            #agent_infos=stack_tensor_dict_list(agent_infos),
            env_infos=stack_tensor_dict_list(env_infos),
            terminated=done
        )
        paths.append(path)

    mean_eval, std = np.mean(ep_returns), np.std(ep_returns)
    min_eval, max_eval = np.amin(ep_returns), np.amax(ep_returns)
    base_stats = [mean_eval, std, min_eval, max_eval]

    percentile_stats = []
    for p in percentile:
        percentile_stats.append(np.percentile(ep_returns, p))

    full_dist = ep_returns if get_full_dist is True else None
    success = env.evaluate_success(paths, logger=None) ## Don't use the mj_envs logging function

    if not eval_logger is None:
        rwd_sparse = np.mean([np.mean(p['env_infos']['rwd_sparse']) for p in paths]) # return rwd/step
        rwd_dense = np.mean([np.sum(p['env_infos']['rwd_dense'])/env.horizon for p in paths]) # return rwd/step
        eval_logger.log_scalar('eval/rwd_sparse', rwd_sparse, step=epoch)
        eval_logger.log_scalar('eval/rwd_dense', rwd_dense, step=epoch)
        eval_logger.log_scalar('eval/success', success, step=epoch)
    return [base_stats, percentile_stats, full_dist], success

class ObservationWrapper:
    def __init__(self, env_name, visual_keys, encoder):
        self.env = gym.make(env_name, visual_keys=visual_keys)
        self.horizon = self.env.horizon
        self.encoder = encoder

    def reset(self, **kwargs):
        obs = self.env.reset(**kwargs)
        return self.get_obs(obs)

    def step(self, action):
        observation, reward, terminated, info = self.env.step(action)
        return self.get_obs(observation), reward, terminated, info

    def get_obs(self, observation=None):
        if self.encoder == 'proprio':
            proprio_vec = self.env.get_proprioception()[1]
            return proprio_vec
        if len(self.env.visual_keys) > 0:
            visual_obs = self.env.get_exteroception()
            final_visual_obs = None
            for key in self.env.visual_keys:
                if final_visual_obs is None:
                    final_visual_obs = visual_obs[key]
                else:
                    final_visual_obs = np.concatenate((final_visual_obs, visual_obs[key]), axis=-1)
            _, proprio_vec, _ = self.env.get_proprioception()
            observation = np.concatenate((final_visual_obs, proprio_vec))
        else:
            observation = self.env.get_obs() if observation is None else observation
        return observation

    def seed(self, seed):
        return self.env.seed(seed)

    def set_env_state(self, state_dict):
        return self.env.set_env_state(state_dict)

    def evaluate_success(self, paths, logger=None):
        return self.env.evaluate_success(paths, logger=logger)


def make_env(env_name, cam_name, encoder, from_pixels):
    if from_pixels:
        visual_keys = []
        assert encoder in ["vc1s", "vc1l", "r3m18", "rrl18", "rrl50", "r3m50", "2d", "1d", "proprio"]
        if encoder == "1d" or encoder == "2d":
            visual_keys = [f'rgb:{cam_name}:84x84:{encoder}']
        elif encoder == 'proprio':
            visual_keys = []
        else:
            # cam_name is a list of cameras
            if type(cam_name) == ListConfig:
                visual_keys = []
                for cam in cam_name:
                    visual_keys.append(f'rgb:{cam}:224x224:{encoder}')
            else:
                visual_keys = [f'rgb:{cam_name}:224x224:{encoder}']
            print(f"Using visual keys {visual_keys}")
        env = ObservationWrapper(env_name, visual_keys=visual_keys, encoder=encoder)
    else:
        env = gym.make(env_name)
    return env

@hydra.main(config_name="bc.yaml", config_path="config")
def main(job_data: DictConfig):
    OmegaConf.resolve(job_data)
    job_data['policy_size'] = tuple(job_data['policy_size'])
    exp_start  = time.time()
    OUT_DIR = os.getcwd()
    if not os.path.exists(OUT_DIR): os.mkdir(OUT_DIR)
    if not os.path.exists(OUT_DIR+'/iterations'): os.mkdir(OUT_DIR+'/iterations')
    if not os.path.exists(OUT_DIR+'/logs'): os.mkdir(OUT_DIR+'/logs')

    if job_data['from_pixels'] == False:
        job_data['env_name'] = job_data['env_name'].replace('_v2d', '')

    #exp_name = OUT_DIR.split('/')[-1] ## TODO: Customizer for logging
    # Unpack args and make files for easy access
    #logger = DataLog()
    exp_name = job_data['env_name'] + '_pixels' + str(job_data['from_pixels']) + '_' + job_data['encoder']
    logger = WandbLogger(
        exp_name=exp_name,
        config=job_data,
        name=exp_name,
        project=job_data['wandb_project'],
        entity=job_data['wandb_entity'],
        mode=job_data['wandb_mode'],
    )


    ENV_NAME = job_data['env_name']
    EXP_FILE = OUT_DIR + '/job_data.yaml'
    SEED = job_data['seed']

    # base cases
    if 'device' not in job_data.keys(): job_data['device'] = 'cpu'
    assert 'data_file' in job_data.keys()

    yaml_config = OmegaConf.to_yaml(job_data)
    with open(EXP_FILE, 'w') as file: yaml.dump(yaml_config, file)

    env = make_env(
            env_name=job_data["env_name"],
            cam_name=job_data["cam_name"],
            encoder=job_data["encoder"],
            from_pixels=job_data["from_pixels"]
    )
    # ===============================================================================
    # Setup functions and environment
    # ===============================================================================
    control_seed(SEED)
    env.seed(SEED)
    paths_trace = Trace.load(job_data['data_file'])

    bc_paths = []
    for key, path in paths_trace.items():
        path_dict = {}
        traj_len = path['observations'].shape[0]
        obs_list = []
        ep_reward = 0.0
        env.reset()
        init_state_dict = {}
        t0 = time.time()
        for key, value in path['env_infos']['state'].items():
            init_state_dict[key] = value[0]
        env.set_env_state(init_state_dict)
        obs = env.get_obs()
        for step in range(traj_len-1):
            next_obs, reward, done, env_info = env.step(path["actions"][step])
            ep_reward += reward
            obs_list.append(obs)
            obs = next_obs
        t1 = time.time()
        obs_np = np.stack(obs_list, axis=0)
        path_dict['observations'] = obs_np # [:-1]
        path_dict['actions'] = path['actions'][()][:-1]
        path_dict['env_infos'] = {'solved': path['env_infos']['solved'][()]}
        print(f"Time to convert one trajectory: {(t1-t0)/60:4.2f}")
        print("Converted episode reward:", ep_reward)
        print("Original episode reward:", np.sum(path["rewards"]))
        print(key, path_dict['observations'].shape, path_dict['actions'].shape)
        bc_paths.append(path_dict)

    expert_success = env.evaluate_success(bc_paths)
    print(f"{bcolors.BOLD}{bcolors.OKGREEN}{exp_name} {bcolors.ENDC}")
    print(f"{bcolors.BOLD}{bcolors.OKGREEN}Expert Success Rate: {expert_success}. {bcolors.ENDC}")

    observation_dim = bc_paths[0]['observations'].shape[-1]
    action_dim = bc_paths[0]['actions'].shape[-1]
    print(f'Policy obs dim {observation_dim} act dim {action_dim}')
    policy = GMMPolicy(
                 # network_kwargs
                 input_size=observation_dim,
                 output_size=action_dim,
                 hidden_size=job_data['policy_size'][0],
                 num_layers=len(job_data['policy_size']),
                 min_std=0.0001,
                 num_modes=5,
                 activation="softplus",
                 low_eval_noise=False,
                 # loss_kwargs
    )
    set_transforms = False

    # ===============================================================================
    # Model training
    # ===============================================================================
    print(f"{bcolors.OKBLUE}Training BC{bcolors.ENDC}")
    policy.to(job_data['device'])

    bc_agent = BC(
                    bc_paths,
                    policy,
                    epochs=job_data['eval_every_n'],
                    batch_size=job_data['bc_batch_size'],
                    lr=job_data['bc_lr'],
                    loss_type='MLE',
                    save_logs=True,
                    logger=logger,
                    set_transforms=set_transforms,
    )

    for ind in range(0, job_data['bc_epochs'], job_data['eval_every_n']):
        policy.train()
        bc_agent.train()
        # bc_agent.train_h5()

        policy.eval()
        _, success_rate =  evaluate_policy(
                                env=env,
                                policy=policy,
                                eval_logger=logger,
                                epoch=ind+job_data['eval_every_n'],
                                num_episodes=job_data['eval_traj'],
                                seed=job_data['seed'] + 123,
                                verbose=True,
                                device='cpu',
        )
        policy.to(job_data['device'])
        exp_end = time.time()
        print(f"{bcolors.BOLD}{bcolors.OKGREEN}Success Rate: {success_rate}. Time: {(exp_end - exp_start)/60:4.2f} minutes.{bcolors.ENDC}")

    exp_end = time.time()
    print(f"{bcolors.BOLD}{bcolors.OKGREEN}Success Rate: {success_rate}. Time: {(exp_end - exp_start)/60:4.2f} minutes.{bcolors.ENDC}")

    # pickle.dump(bc_agent, open(OUT_DIR + '/iterations/agent_final.pickle', 'wb'))
    pickle.dump(policy, open(OUT_DIR + '/iterations/policy_final.pickle', 'wb'))
    wandb.finish()

if __name__ == '__main__':
    main()
