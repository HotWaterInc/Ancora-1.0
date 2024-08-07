import time
import math
from typing import Dict, TypedDict, Generator, List
from src.action_ai_controller import ActionAIController
from src.global_data_buffer import GlobalDataBuffer, empty_global_data_buffer
from src.modules.save_load_handlers.data_handle import write_other_data_to_file

from src.action_robot_controller import detach_robot_sample_distance, detach_robot_sample_image, \
    detach_robot_teleport_relative, \
    detach_robot_rotate_absolute, detach_robot_rotate_relative, detach_robot_teleport_absolute, \
    detach_robot_rotate_continuous_absolute, detach_robot_forward_continuous, detach_robot_sample_image_inference
import threading
import torch
import time
import torch.nn as nn
import torch.optim as optim
import numpy as np
from src.modules.save_load_handlers.ai_models_handle import save_ai, save_ai_manually, load_latest_ai, \
    load_manually_saved_ai
from src.modules.save_load_handlers.parameters import *
from src.ai.runtime_data_storage.storage_superset2 import StorageSuperset2, thetas_to_radians
from src.ai.runtime_data_storage import Storage
from typing import List, Dict, Union
from src.utils import array_to_tensor
from src.ai.models.base_autoencoder_model import BaseAutoencoderModel
from src.ai.evaluation.evaluation import evaluate_reconstruction_error, evaluate_distances_between_pairs, \
    evaluate_adjacency_properties
from src.ai.evaluation.evaluation import evaluate_reconstruction_error, evaluate_distances_between_pairs, \
    evaluate_adjacency_properties, evaluate_reconstruction_error_super, evaluate_distances_between_pairs_super, \
    evaluate_adjacency_properties_super

from src.modules.policies.data_collection import get_position, get_angle
from src.modules.policies.testing_image_data import test_images_accuracy, process_webots_image_to_embedding, \
    squeeze_out_resnet_output
from src.modules.policies.utils_lib import webots_radians_to_normal, radians_to_degrees

import torch
import torchvision.models as models
import torchvision.transforms as transforms


def load_everything():
    global storage, direction_network
    storage = StorageSuperset2()
    grid_dataset = 5

    storage.load_raw_data_from_others(f"data{grid_dataset}x{grid_dataset}_rotated24_image_embeddings.json")
    storage.load_raw_data_connections_from_others(f"data{grid_dataset}x{grid_dataset}_connections.json")

    direction_network = load_manually_saved_ai(DIRECTION_NETWORK_SS_NAME)
    direction_network.to(device)
    direction_network.eval()


def next_embedding_policy_ab(current_embedding, target_embedding):
    # unstable heuristic
    ab_step = target_embedding - current_embedding
    ab_step = ab_step / torch.norm(ab_step)
    next_embedding = current_embedding + ab_step

    return next_embedding


def find_closest_known_position(current_embedding, theta_percent):
    global storage
    best_embedding_distance = 100000
    best_embedding_name = None
    grid_dataset = 5
    current_theta_percent = theta_percent

    theta_search_index_left = int(current_theta_percent * 24)
    theta_search_index_right = int(current_theta_percent * 24) + 1
    if theta_search_index_right == 24:
        theta_search_index_right = 0

    for i in range(grid_dataset):
        for j in range(grid_dataset):
            target_name = f"{i}_{j}"
            potential_emb_left = storage.get_datapoint_data_tensor_by_name(target_name)[theta_search_index_left].to(
                device)
            potential_emb_right = storage.get_datapoint_data_tensor_by_name(target_name)[theta_search_index_right].to(
                device)

            distance_left_embedding = torch.norm(potential_emb_left - current_embedding, p=2, dim=0).item()
            distance_right_embedding = torch.norm(potential_emb_right - current_embedding, p=2, dim=0).item()

            if distance_left_embedding < best_embedding_distance:
                best_embedding_distance = distance_left_embedding
                best_embedding_name = target_name

            if distance_right_embedding < best_embedding_distance:
                best_embedding_distance = distance_right_embedding
                best_embedding_name = target_name

    return best_embedding_name


def next_embedding_policy_search_closest(current_embedding, current_theta_percent, target_embedding_i,
                                         target_embedding_j):
    # global THRESHOLD, prev_best_distance
    THRESHOLD = 0.5
    prev_best_distance = 100000
    # print("target embedding", target_embedding_i, target_embedding_j)

    global storage
    # searches the closest embedding to current embedding at a minimum distance from target embedding ( distance recorded wise )

    all_connections = storage.get_all_adjacent_data()
    target_name = f"{target_embedding_i}_{target_embedding_j}"
    # assumes 24 rotations in data

    theta_search_index_left = int(current_theta_percent * 24)
    theta_search_index_right = int(current_theta_percent * 24) + 1

    potential_current_embedding = None
    best_distance = 100000

    bestij = None

    # try for target position
    potential_emb_left = storage.get_datapoint_data_tensor_by_name(target_name)[theta_search_index_left].to(device)
    potential_emb_right = storage.get_datapoint_data_tensor_by_name(target_name)[theta_search_index_right].to(device)

    distance_left_embedding = torch.norm(potential_emb_left - current_embedding, p=2, dim=0).item()
    distance_right_embedding = torch.norm(potential_emb_right - current_embedding, p=2, dim=0).item()

    if distance_left_embedding < THRESHOLD:
        potential_current_embedding = potential_emb_left
        best_distance = 0
        bestij = target_name
    if distance_right_embedding < THRESHOLD:
        potential_current_embedding = potential_emb_right
        best_distance = 0
        bestij = target_name

    for connection in all_connections:
        potential_current_embedding_name = None

        if connection["start"] == target_name:
            potential_current_embedding_name = connection["end"]
        elif connection["end"] == target_name:
            potential_current_embedding_name = connection["start"]

        if potential_current_embedding_name is None:
            continue

        potential_current_embedding_left = storage.get_datapoint_data_tensor_by_name(potential_current_embedding_name)[
            theta_search_index_left].to(device)
        potential_current_embedding_right = storage.get_datapoint_data_tensor_by_name(potential_current_embedding_name)[
            theta_search_index_right].to(device)
        current_distance = connection["distance"]

        distance_left_embedding = torch.norm(potential_current_embedding_left - current_embedding, p=2, dim=0).item()
        distance_right_embedding = torch.norm(potential_current_embedding_right - current_embedding, p=2, dim=0).item()

        # print(connection)
        # print("distances", distance_left_embedding, distance_right_embedding)

        if current_distance <= best_distance and current_distance <= prev_best_distance:
            found_sol = False

            if distance_left_embedding < THRESHOLD:
                potential_current_embedding = potential_current_embedding_left
                found_sol = True

            if distance_right_embedding < THRESHOLD:
                potential_current_embedding = potential_current_embedding_right
                found_sol = True

            if distance_left_embedding < THRESHOLD and distance_right_embedding < THRESHOLD:
                found_sol = True
                if distance_left_embedding < distance_right_embedding:
                    potential_current_embedding = potential_current_embedding_left
                else:
                    potential_current_embedding = potential_current_embedding_right

            if found_sol:
                bestij = potential_current_embedding_name
                best_distance = current_distance
                prev_best_distance = best_distance

    if potential_current_embedding is None:
        print("NO POTENTIAL CURRENT EMBEDDING FOUND, INCREASING THRESHOLD")
        THRESHOLD += 1
        potential_current_embedding = current_embedding
    else:
        print("BESTIJ", bestij, "BEST DISTANCE", best_distance, "THRESHOLD", THRESHOLD)

    return potential_current_embedding


import random


def djakstra_search(current_uid, target_uid):
    global storage
    all_connections = storage.get_all_adjacent_data()
    # filter for distance = 1
    all_connections = [connection for connection in all_connections if connection["distance"] == 1]
    # shuffle connections
    random.shuffle(all_connections)

    visited = {}
    distances = {}
    previous = {}

    for connection in all_connections:
        start = connection["start"]
        end = connection["end"]
        distance = connection["distance"]

        if start not in visited:
            visited[start] = False
            distances[start] = 100000
            previous[start] = None

        if end not in visited:
            visited[end] = False
            distances[end] = 100000
            previous[end] = None

    distances[current_uid] = 0

    while True:
        min_distance = 100000
        min_uid = None

        for uid in visited:
            if visited[uid] is False and distances[uid] < min_distance:
                min_distance = distances[uid]
                min_uid = uid

        if min_uid is None:
            break

        visited[min_uid] = True

        for connection in all_connections:
            start = connection["start"]
            end = connection["end"]
            distance = connection["distance"]

            if start == min_uid:
                if distances[min_uid] + distance < distances[end]:
                    distances[end] = distances[min_uid] + distance
                    previous[end] = min_uid

            if end == min_uid:
                if distances[min_uid] + distance < distances[start]:
                    distances[start] = distances[min_uid] + distance
                    previous[start] = min_uid

    path = []
    current = target_uid
    while current is not None:
        path.append(current)
        current = previous[current]

    path.reverse()
    return path


def average_angles_directions(angles):
    real = 0
    imaginary = 0

    for angle in angles:
        cosx = math.cos(angle)
        sinx = math.sin(angle)
        real += cosx
        imaginary += sinx

    final_angle = math.atan2(imaginary, real)
    if final_angle < 0:
        final_angle += 2 * math.pi

    return final_angle


def policy_thetas_navigation_next_manifold(current_manifold: torch.Tensor, next_manifold: torch.Tensor):
    global direction_network

    direction_network = direction_network.to(device)
    direction_network.eval()

    thetas_direction = direction_network(current_manifold.unsqueeze(0), next_manifold.unsqueeze(0)).squeeze(0)
    final_angle = thetas_to_radians(thetas_direction)
    return final_angle


def policy_thetas_navigation_next_close_target(index_rotation, current_embedding, next_target: str):
    global direction_network

    next_embeddings = storage.get_datapoint_data_tensor_by_name(next_target).to(device)
    direction_network = direction_network.to(device)
    current_embedding = squeeze_out_resnet_output(current_embedding)

    # clone current embedding 24 times
    current_embeddings = current_embedding.unsqueeze(0).repeat(24, 1)

    thetas_directions = direction_network(current_embeddings, next_embeddings)
    angles = [thetas_to_radians(direction) for direction in thetas_directions]

    final_angle = average_angles_directions(angles)
    return final_angle


def storage_to_manifold():
    global storage
    global autoencoder
    autoencoder = load_manually_saved_ai(AUTOENCODER_NAME)
    autoencoder.eval()
    autoencoder = autoencoder.to(device)

    storage.set_permutor(autoencoder)
    storage.build_permuted_data_raw_abstraction_autoencoder_manifold()


def navigation_image_1camera_vae() -> Generator[None, None, None]:
    load_everything()
    storage_to_manifold()
    global storage, direction_network, autoencoder

    autoencoder.eval()

    target_reached = False

    while True:
        # takes i j from user
        i = int(input("Enter i: "))
        j = int(input("Enter j: "))
        print("i:", i, "j:", j)
        target_reached = False

        while target_reached is False:
            time.sleep(0.5)
            detach_robot_sample_image_inference()
            yield

            global_data_buffer: GlobalDataBuffer = GlobalDataBuffer.get_instance()
            buffer = global_data_buffer.buffer
            # print("Buffer:", buffer)
            image_data = buffer["data"]
            empty_global_data_buffer()

            nd_array_data = np.array(image_data)
            angle = buffer["params"]["angle"]
            angle = webots_radians_to_normal(angle)

            angle_percent = angle / (2 * math.pi)
            print("ANGLE PERCENT", angle_percent)

            current_embedding = process_webots_image_to_embedding(nd_array_data).to(device)
            current_embedding = squeeze_out_resnet_output(current_embedding)
            current_manifold = autoencoder.encoder_inference(current_embedding.unsqueeze(0)).squeeze()

            closest = find_closest_known_position(current_manifold, angle_percent)
            if closest == f"{i}_{j}":
                print("TARGET REACHED")
                break

            # path = djakstra_search(closest, f"{i}_{j}")
            # print("traversing:", closest, path[1])
            # print(path)
            #
            # next_embedding = path[1]
            # index_rotation = int(angle_percent * 24)
            # final_angle = policy_thetas_navigation_next_close_target(index_rotation, current_manifold, next_embedding)

            target_manifold = storage.get_datapoint_data_tensor_by_name(f"{i}_{j}")[0].to(device)
            next_manifold_location = next_embedding_policy_ab(current_manifold, target_manifold)

            final_angle = policy_thetas_navigation_next_manifold(current_manifold, next_manifold_location)

            print("Angle:", radians_to_degrees(final_angle))

            # add angle noise
            # angle += np.random.normal(0, 0.1)

            detach_robot_rotate_absolute(final_angle)
            yield
            detach_robot_forward_continuous(0.25)
            yield


storage: StorageSuperset2 = None
direction_network = None
autoencoder: BaseAutoencoderModel = None

# DIRECTION_NETWORK_PATH = "direction_image_raw_v3.pth"
DIRECTION_NETWORK_SS_NAME = "direction_SS_thetas_postautoencod_saved.pth"
AUTOENCODER_NAME = "camera1_full_forced_saved.pth"

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
