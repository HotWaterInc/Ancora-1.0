import math

import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
from src.ai.runtime_data_storage.storage_superset2 import StorageSuperset2, RawConnectionData
from src.modules.save_load_handlers.ai_models_handle import load_manually_saved_ai, save_ai_manually
from src.ai.models.base_autoencoder_model import BaseAutoencoderModel
from src.utils import array_to_tensor
from typing import List
import torch.nn.functional as F

if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")


class DirectionNetworkUp(nn.Module):
    def __init__(self, input_size=24, embedding_size=96, num_heads=8, dropout_rate=0.2, output_size=36):
        super(DirectionNetworkUp, self).__init__()
        self.input_size = input_size
        self.embedding_size = embedding_size

        # Initial embedding layer
        self.input_embed = nn.Linear(input_size, embedding_size)
        self.input_embed2 = nn.Linear(embedding_size, embedding_size)

        self.layers = nn.ModuleList()
        for _ in range(2):
            self.layers.append(nn.ModuleDict({
                'attention': nn.MultiheadAttention(embed_dim=embedding_size, num_heads=num_heads),
                'fc1': nn.Linear(embedding_size, embedding_size),
                'fc2': nn.Linear(embedding_size, embedding_size),
                'tanh': nn.Tanh(),
                'dropout': nn.Dropout(dropout_rate)
            }))

        # Final output layers
        self.output_layer = nn.Linear(embedding_size, embedding_size)
        self.output_layer2 = nn.Linear(embedding_size, output_size)
        self.activation = nn.LeakyReLU()
        self.tanh = nn.Tanh()

    def forward(self, x, y):
        # Combine input and thetas
        diff = y - x

        # Initial embedding
        x = self.input_embed(diff)
        x = self.activation(x)
        x = self.input_embed2(x)
        x = self.tanh(x)

        # Process through attention and additional layers
        for layer in self.layers:
            residual = x

            # Attention
            x_att = x.unsqueeze(0)  # Add sequence dimension
            x_att, _ = layer['attention'](x_att, x_att, x_att)
            x_att = x_att.squeeze(0)  # Remove sequence dimension

            # Fully connected layers with dropout
            x = self.activation(layer['fc1'](x))
            x = layer['dropout'](x)
            x = self.tanh(layer['fc2'](x))  # Add tanh to one of the FNNs
            x = layer['dropout'](x)

            # Add residual connection
            x = x + residual

        # Final output
        x = self.output_layer(x)
        x = self.activation(x)
        x = self.output_layer2(x)
        return x


def direction_to_degrees(direction):
    y = direction[1]
    x = direction[0]
    degrees = None

    if y == 1:
        degrees = 0
    elif y == -1:
        degrees = 180
    elif x == 1:
        degrees = 270
    elif x == -1:
        degrees = 90

    if degrees == None:
        raise Exception("Shit went wrong as always dir to deg")

    return degrees


def radians_to_degrees(radians):
    return radians * 180 / np.pi


def embedding_policy(data):
    global autoencoder
    start_embedding = data
    # start_embedding = autoencoder.encoder_inference(data)
    return start_embedding


def direction_loss(direction_network, sample_rate=64):
    global storage
    loss = torch.tensor(0.0)

    # get connections
    # sample for each connection random rotation
    # pass those to autoencoder, get the output, add to batch
    # add to batch
    # add to other batch expected direction

    datapoints: List[str] = storage.sample_n_random_datapoints(sample_rate)
    start_embeddings_batch = []
    end_embeddings_batch = []

    # expected_directions_batch = []
    target_thetas_batch = []
    targets = []

    for datapoint in datapoints:
        connections_to_point: List[RawConnectionData] = storage.get_datapoint_adjacent_connections(datapoint)
        for j in range(len(connections_to_point)):
            start = connections_to_point[j]["start"]
            end = connections_to_point[j]["end"]
            direction = connections_to_point[j]["direction"]
            direction = torch.tensor(direction, dtype=torch.float32, device=device)

            l2_direction = torch.norm(direction, p=2, dim=0, keepdim=True)
            direction = direction / l2_direction
            direction = direction.to(device)

            start_data = storage.get_datapoint_data_tensor_by_name_permuted(start)
            end_data = storage.get_datapoint_data_tensor_by_name_permuted(end)

            start_embedding = embedding_policy(start_data)
            end_embedding = embedding_policy(end_data)

            start_embeddings_batch.append(start_embedding)
            end_embeddings_batch.append(end_embedding)

            degrees = direction_to_degrees(direction)
            theta_percent = degrees / 360
            target = int(theta_percent * 36)
            targets.append(target)

            # thetas = build_thetas(theta_percent, 36)
            # target_thetas_batch.append(thetas)

            # print(theta_percent, degrees)
            # print(start, end, direction)
            # print(thetas)
            # print(thetas_to_degree(thetas))

    start_embeddings_batch = torch.stack(start_embeddings_batch).to(device)
    end_embeddings_batch = torch.stack(end_embeddings_batch).to(device)

    # target_thetas_batch = torch.stack(target_thetas_batch).to(device)
    # target_thetas_batch = F.normalize(target_thetas_batch, p=1, dim=1)

    # In softmax probability form
    output_thetas_batch = direction_network(start_embeddings_batch, end_embeddings_batch)
    # applies log to the output thetas
    # log_predicted_probs = torch.log_softmax(output_thetas_batch, dim=1, dtype=torch.float32)

    # Define the KL Divergence Loss function
    # criterion = torch.nn.KLDivLoss(reduction='batchmean')
    criterion = torch.nn.CrossEntropyLoss()
    targets = torch.tensor(targets, dtype=torch.long, device=device)

    # Calculate the loss
    # loss = criterion(log_predicted_probs, target_thetas_batch)
    loss = criterion(output_thetas_batch, targets)

    # criterion = nn.L1Loss()
    # loss = criterion(output_thetas_batch, target_thetas_batch)

    return loss


def train_direction_ai(direction_network, num_epochs):
    optimizer = optim.Adam(direction_network.parameters(), lr=0.004)

    scale_direction_loss = 1

    epoch_average_loss = 0
    epoch_print_rate = 100

    storage.build_permuted_data_random_rotations_rotation0()

    for epoch in range(num_epochs):
        # if (epoch % 10 == 0):
        #     storage.build_permuted_data_random_rotations()

        epoch_loss = 0.0

        optimizer.zero_grad()

        loss = direction_loss(direction_network) * scale_direction_loss
        loss.backward()

        optimizer.step()

        epoch_loss += loss.item()
        epoch_average_loss += epoch_loss

        if epoch % epoch_print_rate == 0 and epoch != 0:
            epoch_average_loss /= epoch_print_rate
            print(f'Epoch [{epoch}/{num_epochs}], Loss: {epoch_average_loss:.4f}')
            epoch_average_loss = 0  # Reset for the next average calculation

    return direction_network


def thetas_to_degree(thetas):
    length = len(thetas)
    degree = 0
    current_degree = 0

    degree_step = 360 / length
    # take first 2 biggest thetas
    a, b = 0, 0

    for i in range(len(thetas)):
        if thetas[i] > thetas[a]:
            b = a
            a = i
        elif thetas[i] > thetas[b]:
            b = i

    current_degree = a * degree_step
    degree += current_degree * thetas[a]
    current_degree = b * degree_step
    degree += current_degree * thetas[b]

    degree /= (thetas[a] + thetas[b])
    return degree


def run_tests_mini(direction_network):
    global storage, autoencoder

    direction_network = direction_network.to(device)
    autoencoder = autoencoder.to(device)

    storage.build_permuted_data_random_rotations()
    datapoints: List[str] = storage.get_all_datapoints()
    output_diffl1 = 0.0
    output_diffl2 = 0.0

    start_embedding_batch = []
    end_embedding_batch = []
    expected_direction_batch = []

    win = 0
    lose = 0

    for datapoint in datapoints:
        connections_to_point: List[RawConnectionData] = storage.get_datapoint_adjacent_connections(datapoint)

        for j in range(len(connections_to_point)):
            start = connections_to_point[j]["start"]
            end = connections_to_point[j]["end"]
            direction = connections_to_point[j]["direction"]

            direction = torch.tensor(direction, dtype=torch.float32, device=device)
            l2_direction = torch.norm(direction, p=2, dim=0, keepdim=True)
            direction = direction / l2_direction

            start_data = storage.get_datapoint_data_tensor_by_name_permuted(start).to(device)
            end_data = storage.get_datapoint_data_tensor_by_name_permuted(end).to(device)

            start_embedding = embedding_policy(start_data).unsqueeze(0)
            end_embedding = embedding_policy(end_data).unsqueeze(0)

            pred_direction_thetas = direction_network(start_embedding, end_embedding).squeeze(0)
            pred_direction_thetas = F.softmax(pred_direction_thetas, dim=0)

            angle_or = direction_to_degrees(direction)
            angle_predicted = thetas_to_degree(pred_direction_thetas.cpu().detach().numpy())
            angle_original = thetas_to_degree(build_thetas(direction_to_degrees(direction) / 360, 36))

            if math.fabs(angle_original - angle_predicted) < 10:
                win += 1
            else:
                lose += 1

            if start == "1_1" or start == "6_6":
                print("pair ", start, end, angle_or)
                print("true direction", direction)
                print("expected direction", build_thetas_2(angle_predicted / 360, 36))
                print("expected direction normalized",
                      F.normalize(build_thetas_2(angle_predicted / 360, 36), dim=0, p=1))
                print("pred direction", pred_direction_thetas)
                print("angle original calculated", angle_original)
                print(angle_predicted)

            # start_embedding_batch.append(start_embedding)
            # end_embedding_batch.append(end_embedding)
            # expected_direction_batch.append(direction)

    print("Win", win)
    print("Lose", lose)
    print("Win rate", win / (win + lose))

    # start_embedding_batch = torch.stack(start_embedding_batch).to(device)
    # end_embedding_batch = torch.stack(end_embedding_batch).to(device)
    # direction_pred = direction_network(start_embedding_batch, end_embedding_batch)
    # expected_direction_batch = torch.stack(expected_direction_batch).to(device)

    # print(direction_pred)
    # print(expected_direction_batch)


def run_tests(direction_network):
    global storage, autoencoder
    ITERATIONS = 10

    total_l1_loss = 0.0
    total_l2_loss = 0.0
    direction_network = direction_network.to(device)
    autoencoder = autoencoder.to(device)

    for i in range(ITERATIONS):
        storage.build_permuted_data_random_rotations()
        datapoints: List[str] = storage.get_all_datapoints()
        output_diffl1 = 0.0
        output_diffl2 = 0.0

        start_embedding_batch = []
        end_embedding_batch = []
        expected_thetas_batch = []
        expected_direction_angles = []

        for datapoint in datapoints:
            connections_to_point: List[RawConnectionData] = storage.get_datapoint_adjacent_connections(datapoint)

            for j in range(len(connections_to_point)):
                start = connections_to_point[j]["start"]
                end = connections_to_point[j]["end"]
                direction = connections_to_point[j]["direction"]

                direction = torch.tensor(direction, dtype=torch.float32, device=device)
                l2_direction = torch.norm(direction, p=2, dim=0, keepdim=True)
                direction = direction / l2_direction

                start_data = storage.get_datapoint_data_tensor_by_name_permuted(start).to(device)
                end_data = storage.get_datapoint_data_tensor_by_name_permuted(end).to(device)
                start_embedding = autoencoder.encoder_inference(start_data)
                end_embedding = autoencoder.encoder_inference(end_data)

                start_embedding_batch.append(start_embedding)
                end_embedding_batch.append(end_embedding)

                angles = direction_to_degrees(direction.cpu().detach().numpy())
                thetas = build_thetas(angles / 360, 36)

                expected_thetas_batch.append(thetas)

        start_embedding_batch = torch.stack(start_embedding_batch).to(device)
        end_embedding_batch = torch.stack(end_embedding_batch).to(device)

        predicted_thetas = direction_network(start_embedding_batch, end_embedding_batch)
        predicted_thetas = F.softmax(predicted_thetas, dim=1)

        expected_thetas_batch = torch.stack(expected_thetas_batch).to(device)
        expected_thetas_batch = F.normalize(expected_thetas_batch, p=1, dim=1)

        # print(direction_pred)
        # print(expected_direction_batch)

        l1_loss_point = torch.mean(torch.norm(predicted_thetas - expected_thetas_batch, p=1, dim=1))
        l2_loss_point = torch.mean(torch.norm(predicted_thetas - expected_thetas_batch, p=2, dim=1))

        output_diffl1 += l1_loss_point.item()
        output_diffl2 += l2_loss_point.item()

        # print(f"Test {i + 1}/{ITERATIONS} L1 Loss: {output_diffl1:.4f}, L2 Loss: {output_diffl2:.4f}")
        total_l1_loss += output_diffl1
        total_l2_loss += output_diffl2

    total_l1_loss /= ITERATIONS
    total_l2_loss /= ITERATIONS
    print(f"Total L1 Loss: {total_l1_loss:.4f}, Total L2 Loss: {total_l2_loss:.4f}")


def run_new_ai():
    direction_network = DirectionNetworkUp().to(device)
    train_direction_ai(direction_network, num_epochs=3000)
    save_ai_manually("direction_networkup", direction_network)
    run_tests_mini(direction_network)
    run_tests(direction_network)


def run_loaded_ai():
    direction_network = load_manually_saved_ai("direction_network_working2.pth")

    run_tests_mini(direction_network)
    run_tests(direction_network)


def run_direction_network():
    global storage
    global permutor
    global autoencoder

    storage = StorageSuperset2()
    permutor = load_manually_saved_ai("permutor_deshift_working.pth")
    autoencoder = load_manually_saved_ai("autoencodPerm25k_w.pth")

    storage.load_raw_data_from_others("data8x8_rotated20.json")
    storage.load_raw_data_connections_from_others("data8x8_connections.json")
    storage.normalize_all_data_super()
    storage.tanh_all_data()
    storage.set_permutor(permutor)
    storage.build_permuted_data_raw_with_thetas()

    run_new_ai()
    # run_loaded_ai()


storage: StorageSuperset2 = None
autoencoder: BaseAutoencoderModel = None