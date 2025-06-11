import itertools
import sys
import json
import os
import argparse
import random
import sys
import numpy as np
import torch
from tqdm import tqdm
from typing import Any, Dict, Optional, Sequence
sys.path.append("../")
from env.scene.base_scene import Scene
from env.agent.mec_kinova import MecKinova
from utils.io import dict2json
from utils.transform import SE3


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--robot', type=str, default='MecKinova', help='robot name, such as MecKinova or Franka')
    p.add_argument('--task', type=str, default='pick', help='task name, such as pick, or place')
    p.add_argument('--result_dir', type=str, help='inference result directory path')
    p.add_argument('--dataset_test_dir', type=str, help='dataset test directory')

    opt = p.parse_args()
    return opt

def percent_true(arr: Sequence) -> float:
    return 100 * np.count_nonzero(arr) / len(arr)

def add_metric(group, key, value):
    group[key] = group.get(key, []) + [value]

def eval_metrics(group: Dict[str, Any]) -> Dict[str, float]:
    # There was a problem with the previous code, so let's rework it here
    group["physical_violations"] = (
        group["collision"] 
        or group["joint_limit_violation"]
        or group["self_collision"]
    )
    group["physical_success"] = [not x for x in group["physical_violations"]]
    ## --------------------------------------------------------------------
    number = np.sum(group["number"])
    physical_success = percent_true(group["physical_success"])
    physical = percent_true(group["physical_violations"])
    config_smoothness = np.mean(group["config_smoothness"])
    eff_smoothness = np.mean(group["eff_smoothness"])
    all_eff_position_path_lengths = np.asarray(group["eff_position_path_length"])
    all_eff_orientation_path_lengths = np.asarray(
        group["eff_orientation_path_length"]
    )
    all_times = np.asarray(group["time"])

    is_smooth = percent_true(
        np.logical_and(
            np.asarray(group["config_smoothness"]) < -1.6,
            np.asarray(group["eff_smoothness"]) < -1.6,
        )
    )

    physical_successes = group["physical_success"]

    physical_success_position_path_lengths = all_eff_position_path_lengths[
        list(physical_successes)
    ]
    physical_success_orientation_path_lengths = all_eff_orientation_path_lengths[
        list(physical_successes)
    ]
    
    if len(physical_success_position_path_lengths) > 0:
        eff_position_path_length = (
            np.mean(physical_success_position_path_lengths),
            np.std(physical_success_position_path_lengths),
        )
    else:
        eff_position_path_length = (0, 0)

    task_success = np.asarray(group["task_success"])

    is_success = percent_true(
        np.logical_and(physical_successes, task_success)
    )

    if len(physical_success_orientation_path_lengths) > 0:
        eff_orientation_path_length = (
            np.mean(physical_success_orientation_path_lengths),
            np.std(physical_success_orientation_path_lengths),
        )
    else:
        eff_orientation_path_length = (0, 0)    

    physical_success_times = all_times[list(group["physical_success"])]

    if len(physical_success_times) > 0:
        time = (
            np.mean(physical_success_times),
            np.std(physical_success_times),
        )
    else:    
        time = (0, 0)

    collision = percent_true(group["collision"])
    joint_limit = percent_true(group["joint_limit_violation"])
    self_collision = percent_true(group["self_collision"])
    depths = np.array(
        list(itertools.chain.from_iterable(group["collision_depths"]))
    )
    if len(depths) == 0: depths = [0]
    all_num_steps = np.asarray(group["num_steps"])
    physical_success_num_steps = all_num_steps[list(physical_successes)]
    if len(physical_success_num_steps) > 0:
        step_time = (
            np.mean(physical_success_times / physical_success_num_steps),
            np.std(physical_success_times / physical_success_num_steps),
        )
    else:
        step_time = (0, 0)

    return {
        "time": time,
        "number": number,
        "step time": step_time,
        "is success": is_success,
        "physical_success": physical_success,
        "env collision": collision,
        "self collision": self_collision,
        "joint violation": joint_limit,
        "physical violations": physical,
        "average collision depth": 100 * np.mean(depths),
        "median collision depth": 100 * np.median(depths),
        "is smooth": is_smooth,
        "average config sparc": config_smoothness,
        "average eff sparc": eff_smoothness,
        "eff position path length": eff_position_path_length,
        "eff orientation path length": eff_orientation_path_length,
    }

def print_metrics(group: Dict[str, Any]):
    metrics = eval_metrics(group)
    print(f"% Success: {metrics['is success']:4.2f}")
    print(f"% With Environment Collision: {metrics['env collision']:4.2f}")
    print(f"% With Self Collision: {metrics['self collision']:4.2f}")
    print(f"% With Joint Limit Violations: {metrics['joint violation']:4.2f}")
    print(f"Average Collision Depth (cm): {metrics['average collision depth']}")
    print(f"Median Collision Depth (cm): {metrics['median collision depth']}")
    print(f"Average Config SPARC: {metrics['average config sparc']:4.2f}")
    print(f"Average End Eff SPARC: {metrics['average eff sparc']:4.2f}")
    print(f"% Smooth: {metrics['is smooth']:4.2f}")
    print(
        "Average End Eff Position Path Length:"
        f" {metrics['eff position path length'][0]:4.2f}"
        f" ± {metrics['eff position path length'][1]:4.2f}"
    )
    print(
        "Average End Eff Orientation Path Length:"
        f" {metrics['eff orientation path length'][0]:4.2f}"
        f" ± {metrics['eff orientation path length'][1]:4.2f}"
    )
    print(f"Average Time: {metrics['time'][0]:4.2f} ± {metrics['time'][1]:4.2f}")
    print(
        "Average Time Per Step (Not Always Valuable):"
        f" {metrics['step time'][0]:4.6f}"
        f" ± {metrics['step time'][1]:4.6f}"
    )

def get_metrics(group: Dict[str, Any]):
    metrics = eval_metrics(group)
    return {
        "% Success": f"{metrics['is success']:4.2f}",
        "Number": f"{metrics['number']}",
        "% With Environment Collision": f"{metrics['env collision']:4.2f}",
        "% With Self Collision": f"{metrics['self collision']:4.2f}",
        "% With Joint Limit Violations": f"{metrics['joint violation']:4.2f}",
        "Average Collision Depth (cm)": f"{metrics['average collision depth']}",
        "Median Collision Depth (cm)": f"{metrics['median collision depth']}",
        "Average Config SPARC": f"{metrics['average config sparc']:4.2f}",
        "Average End Eff SPARC": f"{metrics['average eff sparc']:4.2f}",
        "% Smooth": f"{metrics['is smooth']:4.2f}",
        "Average End Eff Position Path Length": f"{metrics['eff position path length'][0]:4.2f} ± {metrics['eff position path length'][1]:4.2f}",
        "Average End Eff Orientation Path Length": f" {metrics['eff orientation path length'][0]:4.2f} ± {metrics['eff orientation path length'][1]:4.2f}",
        "Average Time": f"{metrics['time'][0]:4.2f} ± {metrics['time'][1]:4.2f}",
        "Average Time Per Step (Not Always Valuable)": f" {metrics['step time'][0]:4.6f} ± {metrics['step time'][1]:4.6f}"
    }

def save_metrics(save_path: str, eval_group: Dict[str, Any]):
    item = {}
    for key in eval_group.keys():
        item[key] = get_metrics(eval_group[key])
    dict2json(save_path, item)

def calculate_iou(rect1, rect2, union_type:Optional[str]=None):
    """ Calculate the Intersection over Union (IOU) of two rectangles.

    Args:
        rect1 [list or tuple]: The first rectangle, specified as [x_min, x_max, y_min, y_max].
        rect2 [list or tuple]: The second rectangle, specified as [x_min, x_max, y_min, y_max].
        union_type [Optional[str]]: The type of union area to use for IOU calculation. 
            If None, use the standard union (area1 + area2 - intersection). 
            If 'area1', use only the area of rect1 as the union. 
            If 'area2', use only the area of rect2 as the union.

    Return:
        np.float64: The Intersection over Union (IOU) value between the two rectangles.
    """
    x_min1, x_max1, y_min1, y_max1 = rect1
    x_min2, x_max2, y_min2, y_max2 = rect2
    # Calculate the (x, y) coordinates of the intersection rectangle
    x_min_inter = max(x_min1, x_min2)
    x_max_inter = min(x_max1, x_max2)
    y_min_inter = max(y_min1, y_min2)
    y_max_inter = min(y_max1, y_max2)
    # Calculate the area of the intersection rectangle
    inter_width = max(0, x_max_inter - x_min_inter)
    inter_height = max(0, y_max_inter - y_min_inter)
    inter_area = inter_width * inter_height
    # Calculate the area of both rectangles
    area1 = (x_max1 - x_min1) * (y_max1 - y_min1)
    area2 = (x_max2 - x_min2) * (y_max2 - y_min2)
    # Calculate the area of the union
    if union_type is None:
        union_area = area1 + area2 - inter_area
    elif union_type == 'area1':
        union_area = area1
    elif union_type == 'area2':
        union_area = area2
    # Compute the IOU
    iou = 1.0 * inter_area / union_area if union_area != 0 else 0.0
    return np.array(iou, dtype=np.float64)

def calculate_iom(rect1, rect2, union_type:Optional[str]=None):
    """ Calculates the Intersection over Minimum (IOM) between two rectangles.
        The IOM is defined as the area of intersection divided by the area of the smaller rectangle.

    Args:
        rect1 [tuple or list]: The first rectangle, specified as (x_min, x_max, y_min, y_max).
        rect2 [tuple or list]: The second rectangle, specified as (x_min, x_max, y_min, y_max).
        union_type [Optional[str]]: Reserved for future use; currently unused.

    Return:
        np.ndarray: The IOM value as a numpy float64 array.
    """
    x_min1, x_max1, y_min1, y_max1 = rect1
    x_min2, x_max2, y_min2, y_max2 = rect2
    # Calculate the (x, y) coordinates of the intersection rectangle
    x_min_inter = max(x_min1, x_min2)
    x_max_inter = min(x_max1, x_max2)
    y_min_inter = max(y_min1, y_min2)
    y_max_inter = min(y_max1, y_max2)
    # Calculate the area of the intersection rectangle
    inter_width = max(0, x_max_inter - x_min_inter)
    inter_height = max(0, y_max_inter - y_min_inter)
    inter_area = inter_width * inter_height
    # Calculate the area of both rectangles
    area1 = (x_max1 - x_min1) * (y_max1 - y_min1)
    area2 = (x_max2 - x_min2) * (y_max2 - y_min2)
    union_area = min(area1, area2)
    # Compute the IOU
    iou = 1.0 * inter_area / union_area if union_area != 0 else 0.0
    return np.array(iou, dtype=np.float64)

def eval_pick_result(result_dir: str, test_dir: str, agent: MecKinova):
    object_result_dir = os.path.join(result_dir, 'object')
    place_eval_res_path = os.path.join(result_dir, 'eval_res_tongverse.json')
    with open(os.path.join(place_eval_res_path), "r") as f:
        eval_res_item = json.load(f)

    scene = None; cur_scene_name = None
    eval_group = {}; eval_group["all"] = {}; center_dists = []
    id_iter = tqdm(sorted(os.listdir(object_result_dir), key=lambda x:int(x.split('.')[0])), desc="{0: ^10}".format('ID'), leave=False, mininterval=0.1)
    for id_name in id_iter:
        id = id_name.split('.')[0]
        result_file_path = os.path.join(object_result_dir, id_name)
        with open(os.path.join(result_file_path), "r") as f:
            result_item = json.load(f)
        test_file_path = os.path.join(test_dir, id + '.npy')
        test_data = np.load(test_file_path, allow_pickle=True).item()
        scene_name = test_data['scene']['name']
        obj_link_name = test_data['object']['name']
        object_name = test_data['object']['name'].split('_')[0]
        place_area_bbox = test_data['task']['placement_area']['scene_placement_pc']['bbox_test']

        if scene_name != cur_scene_name:
            scene = Scene(scene_name)
            cur_scene_name = scene_name

        if 'bottle' in object_name:
            object_name = 'bottle'
        if 'shaker' in object_name:
            object_name = 'shaker'
        if 'knife' in object_name:
            object_name = 'knife'

        # choose iom for success rate computation
        iom_init = eval_res_item[id]['iom_init']
        iom_final = eval_res_item[id]['iom_final']
        obj_final_z = eval_res_item[id]['obj_final_xyz'][-1]
        t_move = eval_res_item[id]['t_move(m)']
        r_move = eval_res_item[id]['r_move(degree)']
        
        if (iom_final > 0.5) and (t_move < 0.02) and (r_move < 3):
            task_success = 1
        else:
            task_success = 0
        
        # center distance
        target_center = place_area_bbox["transform"][:-2,-1]
        T_ow = SE3(xyz=eval_res_item[id]['obj_final_xyz'], quaternion=eval_res_item[id]['obj_final_quaternion'])
        scene.update_object_position_by_transformation_matrix(obj_link_name, T_ow.matrix)
        obj_bbox_center = scene.get_link_in_scene(obj_link_name).bounding_box.transform[:-2,-1]
        center_dist = np.linalg.norm(target_center - obj_bbox_center)
        center_dists.append(center_dist)

        # all results
        add_metric(eval_group["all"], "collision_depths", result_item["collision_depths"])
        add_metric(eval_group["all"], "collision", result_item["collision"])
        add_metric(eval_group["all"], "physical_success", result_item["physical_success"])
        add_metric(eval_group["all"], "physical_violations", result_item["physical_violations"])
        add_metric(eval_group["all"], "joint_limit_violation", result_item["joint_limit_violation"])
        add_metric(eval_group["all"], "self_collision", result_item["self_collision"])
        add_metric(eval_group["all"], "config_smoothness", result_item["config_smoothness"])
        add_metric(eval_group["all"], "eff_smoothness", result_item["eff_smoothness"])
        add_metric(eval_group["all"], "joint_limit_violation", result_item["joint_limit_violation"])
        add_metric(eval_group["all"], "eff_position_path_length", result_item["eff_position_path_length"])
        add_metric(eval_group["all"], "eff_orientation_path_length", result_item["eff_orientation_path_length"])
        add_metric(eval_group["all"], "time", result_item["time"])
        add_metric(eval_group["all"], "num_steps", result_item["num_steps"])
        add_metric(eval_group["all"], "task_success", task_success)
        add_metric(eval_group["all"] , "number", 1)

        # object results
        if object_name not in eval_group.keys():
            eval_group[object_name] = {}
        add_metric(eval_group[object_name] , "collision_depths", result_item["collision_depths"])
        add_metric(eval_group[object_name] , "collision", result_item["collision"])
        add_metric(eval_group[object_name] , "physical_success", result_item["physical_success"])
        add_metric(eval_group[object_name] , "physical_violations", result_item["physical_violations"])
        add_metric(eval_group[object_name] , "joint_limit_violation", result_item["joint_limit_violation"])
        add_metric(eval_group[object_name] , "self_collision", result_item["self_collision"])
        add_metric(eval_group[object_name] , "config_smoothness", result_item["config_smoothness"])
        add_metric(eval_group[object_name] , "eff_smoothness", result_item["eff_smoothness"])
        add_metric(eval_group[object_name] , "joint_limit_violation", result_item["joint_limit_violation"])
        add_metric(eval_group[object_name] , "eff_position_path_length", result_item["eff_position_path_length"])
        add_metric(eval_group[object_name] , "eff_orientation_path_length", result_item["eff_orientation_path_length"])
        add_metric(eval_group[object_name] , "time", result_item["time"])
        add_metric(eval_group[object_name] , "num_steps", result_item["num_steps"])
        add_metric(eval_group[object_name] , "task_success", task_success)
        add_metric(eval_group[object_name] , "number", 1)
        
    save_metrics(os.path.join(result_dir, 'eval_metrics_tongverse.json'), eval_group)
    dict2json(os.path.join(result_dir, 'center_dists_tongverse.json'), center_dists)


if __name__ == "__main__":
    args = parse_args()

    ## set random seed
    seed = 2024
    torch.backends.cudnn.benchmark = False     
    torch.backends.cudnn.deterministic = True
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    agent = MecKinova()
    eval_pick_result(args.result_dir, args.dataset_test_dir, agent)
